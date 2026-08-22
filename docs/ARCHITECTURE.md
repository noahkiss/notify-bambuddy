# Architecture

This document exists for one reason: almost every non-obvious decision in this
codebase is non-obvious because the obvious version is wrong on real hardware.
Without the reasons written down, the next person to read the code will
"simplify" it straight back into the bugs it was written to avoid.

Everything below was verified against BambuBuddy's source
([maziggy/bambuddy](https://github.com/maziggy/bambuddy), AGPL-3.0) and
Notify's Live Activity API ([published reference](https://getnotifyapp.com/apidocs/)),
not against their documentation. Where source and
documentation disagree, that is called out, because the disagreements are the
expensive part.

---

## Shape

```
APISource ──queue─► Coordinator ──► NotifyClient ──► push.getnotifyapp.com
                         │                │
                         ├──► Alerts ─────┘
                         │
                         └──► Store (SQLite, WAL)
                                  │
                          HealthServer (own thread, read only)
```

One module owns each concern:

| Module | Owns |
|---|---|
| `bambuddy_api.py` | The REST API: `BambuBuddyClient`, `status_to_event`, `APISource`. |
| `models.py` | The domain types, and the unit conversion. |
| `coordinator.py` | The state machine, and every call to Notify. |
| `content.py` | What a tile actually says. |
| `notify_client.py` | HTTP, and turning status codes into decisions. |
| `store.py` | What has to survive a restart. |
| `alerts.py` | Ordinary push notifications, and when they are warranted. |
| `config.py` | Validation, loudly, at startup. |
| `redact.py` | Keeping the device token and the API key out of the logs. |
| `health.py` | The local diagnostics endpoint, and what readiness means. |
| `__main__.py` | Startup order, the event loop, and shutdown. |

Threading is deliberately small. The poll loop adds no thread at all: it is an
asyncio task awaiting its HTTP calls on the same loop as everything else. The
health endpoint runs its own thread and only reads. Everything else is single
threaded.

---

## Where the state comes from

State reaches the coordinator from one place: BambuBuddy's own REST API,
polled with an API key. That needs nothing which is not already running. No
change to BambuBuddy's own configuration, one key with one permission.

`BAMBUDDY_URL` unset is a startup error rather than a silent default, because
a service pointed at nothing behaves identically to a service pointed at an
idle printer, and would look fine for days.

| Variable | Default | Notes |
|---|---|---|
| `BAMBUDDY_URL` | none | Where BambuBuddy is |
| `BAMBUDDY_API_KEY` | none | Or `BAMBUDDY_API_KEY_FILE` |
| `BAMBUDDY_POLL_SECONDS` | `10` | Minimum 2, maximum 300 |
| `BAMBUDDY_DISCOVERY_SECONDS` | `300` | How often the printer list is refetched |
| `BAMBUDDY_TIMEOUT` | `10` | Per request |
| `BAMBUDDY_PRINTER_IDS` | empty | Comma separated ids, empty means all |

Two endpoints, both `GET`, both satisfied by the `read_status` permission
alone:

```
GET /api/v1/webhook/queue                 -> printer_id, printer_name
GET /api/v1/webhook/printer/{id}/status   -> the telemetry
```

A real captured status response, verbatim:

```json
{"id":1,"name":"Workshop H2C","connected":true,"state":"RUNNING",
 "current_print":"Articulated Octopus","progress":52.0,"remaining_time":43}
```

That is the whole of what this service gets. The key travels in the
`X-API-Key` **header** and never in a URL, so it cannot be picked up from a
proxy log or a request line, and `redact.py` keeps it out of this service's
own logs.

`remaining_time` is minutes, because BambuBuddy stores the firmware's
`mc_remaining_time` verbatim and this route passes it through untouched. The
next section is about that number.

### What the API does not carry

Four things it would be reasonable to expect and that are simply not there.
Each is a fixed property of this integration rather than a setting, so they
are worth knowing before reading anything below that works around them:

- **Layer numbers.** No layer counter on the tile, ever.
- **Error codes.** A pause cannot be labelled `Filament out`. It says
  `Paused` and stops there.
- **A plate-clear flag.** Nothing says the bed was cleared.
- **Lifecycle events.** A poll returns a state, never an event, so a terminal
  outcome is inferred from the state going inactive rather than reported.

The last one has a consequence worth stating plainly: a print that both starts
and finishes between two polls is never seen. Nothing is left behind to find,
only a printer that read `IDLE` before and reads `IDLE` again.
`BAMBUDDY_POLL_SECONDS` bounds that window, which is the argument for leaving
it near its default rather than raising it out of politeness.

### Printer identity without a serial

The REST API exposes no printer serial. Sessions are keyed by `bb` plus
BambuBuddy's own database id for the printer, so printer `1` is `bb1`. That id
survives a rename, which the printer's name does not.

The trap this leaves is in filtering. `PRINTER_ALLOWLIST` matches the session
key and therefore wants `bb1`, while `BAMBUDDY_PRINTER_IDS` takes the plain
`1`. Prefer `BAMBUDDY_PRINTER_IDS`: it is the poller's own filter and it
speaks the ids the API itself speaks.

### Pacing the poll

`BAMBUDDY_POLL_SECONDS` is the cadence while anything is printing. With every
printer idle the interval is eight times longer, with a 30 second floor, since
the only thing being watched for then is a print appearing at all. After a
failed poll the interval doubles, capped at 120 seconds.

The backoff is computed from the **normal** interval for the current activity,
and is never allowed to be faster than that healthy cadence. The first version
doubled the configured interval alone, which meant that with everything idle a
failing BambuBuddy could be polled every 60 seconds against an 80 second
healthy cadence: the service leaned on it hardest exactly when it was already
broken.

`401` and `403` raise `BambuBuddyAuthError`, which is fatal at startup with
exit code 2 and carries BambuBuddy's own `detail` text rather than a generic
message, because a wrong key and a key without `read_status` are different
problems and BambuBuddy already knows which one it is. Everything else is a
`BambuBuddyError`: not fatal, retried with the backoff above. One unreachable
printer is not a reason to exit.

### Health

The health snapshot gathers all of this under one key, `source`, carrying
`source`, `url`, `printers_known`, `printers_polled`, `polls_succeeded`,
`events_emitted`, `consecutive_failures`, `last_error`,
`seconds_since_success` and `poll_interval`.

`/readyz` is green when at least one poll has ever succeeded and the most
recent one did. Both halves matter: the first stops a service that has never
worked from claiming it is ready, the second stops one that worked an hour ago
from claiming it still does. `/healthz` is liveness only, and answers as long
as the process is up.

### Why not BambuBuddy's webhook notifications

BambuBuddy can push notifications out itself, which is what its Bambark add-on
consumes, and that was evaluated first. Its progress event fires only at the
25%, 50% and 75% milestones, and it reports time remaining as a preformatted
string such as `"2h 14m"`. A Live Activity needs a number to count down from,
and an update rate finer than three per print. A status route polled on a
timer gives both.

---

## The unit conversion

`remaining_time` is in **minutes**.

The BambuBuddy wiki's example payload shows `"remaining_time": 3600` next to
`"progress": 45.5`, which reads unmistakably like seconds. It is not. The
value is the printer firmware's `mc_remaining_time` passed through unchanged,
and BambuBuddy's own other consumer of the same field does:

```python
"remaining_time": self.state.remaining_time * 60  # Convert minutes to seconds
```

The status route does not convert. This service does, in
`PrinterStatus.eta_seconds`, and the conversion is pinned by the first test in
`tests/test_units_and_content.py`. Get it wrong and every countdown is out by
a factor of sixty, silently: the tile appears, looks plausible, and finishes
an hour early.

It has also been measured against a live BambuBuddy instance. Sampling one
printer's status 749 seconds apart, `remaining_time` fell from 74 to 63:

```
elapsed wall clock            749 s
remaining_time                74 -> 63   (a drop of 11)
if the unit were seconds      11 s of print in 749 s   ->  0.01x realtime
if the unit were minutes      660 s of print in 749 s  ->  0.88x realtime
```

A countdown tracks wall clock, so 0.88 is the answer and 0.01 is not. The
shortfall from 1.0 is the printer re-estimating as it goes, which is exactly
why the tile refreshes `endsIn` rather than trusting the first value.

`remaining_time: 0` means **not known yet**, not "finishing now". It reads 0
during pre-print calibration, and BambuBuddy defaults the field to 0 rather
than null. `eta_seconds` returns `None` for that case so no caller can
accidentally send `endsIn: 0`, which Notify rejects with a `400`. On a start
call, a `400` means no tile at all for that print.

---

## Session identity

**One session per printer**, because a printer prints one thing at a time.
There is no printer serial anywhere in the API, so that printer is `bb` plus
BambuBuddy's printer id, as described above.

That single constraint is what makes terminal correlation tractable, and the
alternative fails outright: **no print id is exposed anywhere.** The printer's
own `subtask_id` is parsed by BambuBuddy and then left out of the status
route, which carries `current_print`, a name and not an id. The name is not
even stable within one print: on a multi-plate job it changes at every plate
boundary, so matching on it would leave a tile stranded at 99% while the
printer carried on.

So a terminal outcome for a printer ends the open session for that printer,
full stop. The internal session key (`{printer key}:{detection epoch}`) is
this service's own primary key and is never matched against anything
BambuBuddy returns.

### One job or two? The continuous-running rule

Two things look like "a new print started" and are not:

- The name in `current_print` changes mid-print, which a multi-plate project
  does at every plate boundary.
- Progress falls back towards 0 at that same boundary.

Both happen with the printer reporting RUNNING the whole way through. A
genuinely new print, by contrast, passes through FINISH or IDLE on the way. So
the rule is: **if this printer has been heard from without a gap longer than
the silence threshold, it is still running the same job**, whatever the name
and the percentage say. A name change on a continuously running printer
renames the job and keeps the tile. Only after a gap is either one a new
print.

This rule is applied before anything about the session's delivery state, which
matters for dismissal: a user who swipes the tile away at plate one must not
get it back at plate two.

**A residual gap, stated honestly:** an immediate reprint of the identical
file, dispatched while this service was down long enough to count as a gap, is
genuinely indistinguishable from a plate change. Nothing catches it.

**And a second gap, found by replaying live telemetry.** The rule assumes a
new print passes through `FINISH` or `IDLE`, which is what breaks continuity
and tells it apart from a plate change. A printer that drops back to 0% while
never leaving `RUNNING` therefore reads as a plate change, and the tile
carries over into what may be a different print.

That pattern was observed on a BambuBuddy demo instance: sampled at 1 Hz, a
printer went `RUNNING 95%` straight to `RUNNING 0%` with no intermediate
state. It is recorded here because it is producible, not because it is
expected: on that same instance BambuBuddy's own archive did NOT record a
completion either, because its completion detection has the same requirement
(`should_trigger_completion` needs `FINISH`, `FAILED`, or `IDLE` straight from
`RUNNING`). Both systems agree the print never ended. The rule is left as it
is: a simulator that skips terminal states is not evidence about printers that
do not.

---

## Creation: a running printer is the whole trigger

Nothing announces a print. A poll returns a state, so a tile is created from
state alone: **RUNNING, with no open session, sustained for `RUNNING_GRACE`
seconds.** The grace is there so one flapping reading cannot spawn a tile.

Being the only path is what makes it robust rather than a compromise. A print
already in flight when this service starts, a print started from the printer's
own touchscreen, a print that began while BambuBuddy itself was restarting:
none of them can announce themselves, and all of them are picked up by the
same rule that picks up an ordinary start.

---

## Liveness comes from arrival time

The status route carries no timestamp of its own, so **every reading is
stamped by this service at the moment the response arrives**. A poll is
answered live or it fails, which makes the age of a value exactly the age of
the request that fetched it: a number cannot reach the coordinator unless a
request succeeded seconds ago.

That is the property the freshness gate rests on. A reading whose stamp is
older than `RETAINED_MAX_AGE` (180s) is ignored entirely, and here the gate
cannot fire while the process is healthy, because the stamp is the round trip.
It is kept anyway, because "cannot fire" is a claim about the process rather
than a guarantee: a stalled event loop is exactly the case where it earns its
keep.

What a reading cannot say is whether the printer is still there. Two things
cover that:

1. **Silence, measured from when the newest usable reading arrived.** If
   nothing has arrived for `SILENCE_AFTER` seconds the tile says "Connection
   lost" rather than freezing a lie, and after `ABANDON_AFTER` it ends rather
   than sitting on the Lock Screen until Apple's eight hour ceiling.
2. **`connected: false`.** A status carrying it is treated as "every other
   field in here is a last-seen value". None of its numbers are applied, and
   it does not count as a liveness refresh either: BambuBuddy goes on
   answering with those for a printer that is unreachable, and counting them
   as telemetry means the tile never reaches "Connection lost" and never gets
   abandoned. The reading proves BambuBuddy is alive, not the printer.

There is a piece of luck in the second one worth writing down, because it
looks like an accident until someone changes it. BambuBuddy's own 60 second
staleness detector runs when something asks for a printer's status. In a
headless deployment nobody is watching the web UI, so nothing would ask.
Polling asks, every `BAMBUDDY_POLL_SECONDS`.

The cost of a polled reading belongs in the same breath: between two polls
there is no information at all. That is the trade, and it is a bounded one, up
to `BAMBUDDY_POLL_SECONDS` of latency.

---

## Terminal outcomes are inferred

Nothing reports the end of a print. The printer's state going inactive is the
whole of the evidence, and it is read as:

```
FINISH                  -> completed
FAILED                  -> failed
anything else inactive  -> aborted
```

after the printer has held that state for 90 seconds. The grace is not
politeness. A printer whose link to BambuBuddy blips reads inactive for a poll
or two and then RUNNING again, and closing a tile on the first inactive
reading is how a print with four hours left loses its tile.

The only distinctions available are the ones that field itself makes, which is
why a stopped print's tile says "Stopped" rather than "Cancelled". Nothing on
this route can say who stopped it, and claiming the user did would sometimes
be wrong.

An outcome, once inferred, is still not treated as final:

1. Terminal **content** is written immediately, so the tile reads correctly.
2. A **hold timer** starts (`TERMINAL_HOLD`, 120s).
3. A higher-ranked outcome may overwrite it during the hold. Outcomes are
   ranked, and success outranks everything.
4. The tile only ends when the hold expires.

Terminal state is also **sticky**. Once a session has an outcome, an ordinary
reading cannot resurrect it: the firmware can flicker back through RUNNING
after a finish, and a tile that comes back to life after saying "Done" is
worse than one that ended a few seconds early.

The push notification follows the same logic, and this is where the inference
shows up in what a user actually receives. "Print finished" goes out at once,
because success outranks everything and nothing arriving later could improve
on it. Nothing at all goes out for a failure or a stop: every non-success
outcome here is a reading of a state field rather than a report, and "Print
failed" is too specific a claim to hang on that. For the silent case the "not
responding" alert has already said what is known.

---

## `410` is seven different things

Notify answers `410 Gone` when a tile no longer exists. The reason matters
enormously and is not in the response body, which carries prose. The
machine-readable `endReason` comes from `GET /live-activity/{activityId}`,
which stays readable for a day after a tile ends.

| `endReason` | Meaning | What this service does |
|---|---|---|
| `dismissed` | The user swiped it away | Never recreate it for this print |
| `dead-token` | The tile vanished from the device | Same as dismissed |
| `script` | We ended it | Nothing |
| `never-started` | The gateway believes the push produced no tile | **Never recreate**, at runtime or on restart |
| `overdue` | The countdown lapsed 30 min ago | Recreate |
| `abandoned` | A countdown-less tile went 2h silent | Recreate |
| `expired` | Past Apple's 8-hour ceiling | Recreate |

`never-started` sits with the top group despite sounding like the bottom one,
and that was learned the hard way. A tile iOS displays while the Notify app
stays closed is never reported back either, so from here it is
indistinguishable from one that truly never arrived. Recreating on it put a
second tile beside a live one on real hardware. It is now refused on both the
runtime and the restart paths.

Conflating the dismissal group with the recreate group is how an integration
permanently stops working for a printer whose ETA happened to overrun by half
an hour.
Every `410` is classified before anything is decided. When the reason cannot
be established, the service errs toward **not** suppressing: a missing tile
someone wanted is a bug report, a silently suppressed printer looks like the
whole thing is broken.

---

## Dismissal is our job, not the gateway's

Notify has its own sticky-dismissal protection on the device-addressed upsert.
This service cannot use it.

Supporting several printers means every start passes `new=1`, because the bare
device address would UPDATE another printer's tile whenever exactly one is
live and return a `409` whenever two or more are. And `new=1` deliberately
skips the dismissal branch.

So dismissal is enforced locally: recorded in SQLite, keyed by **printer**,
cleared the moment a genuinely new print is detected. Keyed by printer rather
than by session because a session key is derived from the moment a print was
detected and cannot be reconstructed after a restart, which would make every
upgrade forget every dismissal.

---

## The tile budget

Notify allows **five** live tiles per device. Notify
reports the cap as a `400`, which is the one transient `400` in the API:
every other `400` is a validation failure that will never succeed.

A client that treats all `400`s as permanent gives up on the sixth printer in
a farm forever, even though a slot frees up twenty minutes later. So the cap
is recognised and the session is queued. When a slot frees, it goes to
a print that is actually **running** before one that is paused or silent, and
within that group to whichever has the shortest remaining time, because the
print worth watching is the one about to need attention. A paused print's ETA
is frozen at whatever it said when it stopped moving, so ranking on ETA alone
would let it jump the queue.

The cap also counts tiles that are merely *starting*, which is why the
verification poll matters.

---

## A tile outlives this service's memory of it

The single most important thing to understand about Live Activities here:
**losing the reference to a tile does not remove the tile.** Once iOS has one
on the Lock Screen it stays for up to eight hours by itself, and Notify's own
documentation is blunt about the consequence, that a script which simply stops
calling "strands a frozen 91% in front of the user".

So an activity id is not a cache. It is the only handle that exists for
something already in front of a person, and it is persisted in SQLite for that
reason rather than for convenience. Run without a volume and every restart
abandons whatever is on screen, permanently.

Two further rules follow from how iOS behaves:

- **A tile the user swiped away never comes back.** Dismissal is final for
  that activity, so a service that tries to "refresh" a dismissed tile
  achieves nothing and spends its start budget doing it.
- **A tile must be ENDED, not merely forgotten.** Ending is idempotent, so it
  is always safe to end something that may already be gone. Not ending is
  never safe.

Together those mean a fresh tile must never be created while an old one for
the same printer might still be live. `_reap_stale_tiles` runs before every
start and ends any tile that no session owns and whose title matches the one
this printer's tile would carry. There are several ways to arrive with such a
tile: the gateway reaping one it believed never arrived while iOS displayed it
anyway, a start whose response was lost, or a state database thrown away with
the container.

Two details in that sweep are load-bearing:

- **It matches on the title as BUILT**, not the raw printer name, and it skips
  tiles a live session owns. Two printers named the same thing produce
  indistinguishable titles, which is an ordinary farm setup, and ownership is
  then the only thing that stops one printer reaping the other's tile.
- **It ends by `activityId`, never by device id.** `DELETE` also accepts a
  device id, but that form ends "the device's single live tile", which with
  several printers is a coin flip over which one loses its tile.

## Verifying that a tile actually appeared

A start can be accepted by Notify, accepted by Apple, and still never produce
a tile.

iOS silently throttles push-to-start per app per device once the device's
start credential goes stale, which happens whenever the app is updated or
reinstalled. Nothing reports this: not Apple, not the gateway, not the app.
The gateway notices after 15 minutes and, after two such losses, begins
refusing starts with a `429` and an escalating quiet period (30 minutes, then
3 hours, then 6).

Waiting does not fix it. The counter resets when the phone reports a fresh
credential, which happens when the user opens the Notify app.

So 90 seconds after every start, this service asks whether the tile is
`active` yet. If it is still `starting`, it logs one line naming BOTH
possibilities, and does nothing else.

That restraint is the whole lesson of this section, and it was learned by
getting it wrong twice.

The first version ended the pending tile and started another. The result was
one discarded push every ninety seconds forever: each kept Apple's throttle
shut, and ending the row forfeited the gateway's own recovery, which re-sends
any start still inside its fifteen-minute window once the phone reports a
fresh credential.

The second version stopped starting tiles instead, escalating 15 minutes to an
hour to three. That was worse. A tile is confirmed only when the app reports
that iOS displayed it, and for a background integration the app is normally
closed, so a HEALTHY tile is routinely unconfirmed. Every blocked print then
produced another unconfirmed start on the next attempt, escalating the block
further, while the gateway counted the same events towards its own `429`. On
real hardware a working phone went nearly two hours with no tiles, with the
supposedly missing tile visible the entire time.

An unconfirmed start is therefore **not a signal this service acts on**. The
gateway's `429` is the only thing that blocks starts, because the gateway is
the only party that can see whether the device is receiving them. Updates and
ends are never gated by either. Being wrong in the other direction costs a
duplicate tile at worst, which the stale-tile sweep already handles.

---

## Pacing and the keepalive

Two different mechanisms, easily confused:

**Pacing** suppresses redundant traffic. A progress tick is sent at most once
per `PROGRESS_MIN_INTERVAL` (15s) and only when the payload actually changed.
A **phase** change is exempt: a pause, a lost connection or a completion is
news, and holding it behind a rate limit designed for percentage ticks makes
the tile feel broken.

**The keepalive** is the opposite and must stay that way. Every 20 minutes,
every live tile is refreshed **unconditionally**, ignoring the
redundant-payload check entirely.

It exists because Notify ends any tile without an end time that has gone two
hours without an update, and a tile whose countdown has been cleared (every
paused, silent or ETA-unknown print) is exactly that shape. An unchanged tile
is the case where the keepalive matters most, so making it conditional on
change would defeat its only purpose. `KEEPALIVE_INTERVAL` is capped below two
hours for the same reason.

---

## The trailing slot holds exactly one thing

The Live Activity widget renders one value in its trailing position, in a
fixed priority order:

```
live countdown  >  trailing text  >  status word
```

A tile sent `endsIn` **and** `status: "Paused"` shows the countdown and
nothing else. The status word cannot render while a countdown is live, on any
surface, ever. The middle slot is not used here at all: the only thing that
ever wanted it was a layer counter, and there are no layer numbers to put in
it.

So the phase decides what may be sent:

| Phase | Trailing slot |
|---|---|
| Printing with a known ETA | Countdown |
| Printing with no ETA yet | `Printing` |
| Paused | `Paused` |
| Silent | `Connection lost` |
| Finished | The outcome word |

`status` is sent in every phase anyway, because it is the fallback the widget
reaches for when a countdown lapses. That covers an overrunning ETA for free,
and it is what a print reads as until `remaining_time` first goes above 0.

The corollary that is easy to miss: in the paused and silent phases the
countdown is deliberately **cleared**, not just left out. It outranks the
status word, so a countdown left ticking would hide the one word the tile
exists to show in those phases, and it would be counting down to an estimate
that stopped moving when the printer did.

---

## Progress arrives wrong

BambuBuddy never resets progress at print start, so the first readings of a
new print carry the *last* print's percentage. Without a guard a fresh tile
appears at 97% and then jumps backwards.

So progress is pinned to 0 until the reading has **moved from the one taken
when the session opened, in either direction**. Call that the trust gate: it
does not ask whether a percentage looks plausible, it waits for proof that the
value belongs to this print.

Both directions matter, for different reasons. A drop is the firmware
replacing a finished print's high percentage with the new print's low one,
which is the case the gate was written for. A climb is a print this service
picked up part way through, where the reading at session open was already
genuine. An earlier version accepted only the drop, and a tile then sat at 0%
for an entire print while the countdown beside it ran correctly, which is the
sort of bug a green test suite walks straight past.

Accepting a climb is safe because of what the gate defends against rather than
in spite of it. The stale carry-over is the previous print's final number, and
a final number sits still. It does not rise. Anything that moves at all is
being produced by this print, which is the proof the gate was waiting for all
along.

Progress is also not monotonic. BambuBuddy's strictly-increasing guard applies
to an internal callback, not to the field on this route, which mirrors the
firmware verbatim including its reset to 0 on cancel. That is why a progress
drop is never taken as a new-print signal on its own: the continuous-running
rule above decides that, and it decides it on gaps rather than on numbers.

---

## Pause has no cause

A filament runout and a deliberate pause are **identical** here: both are
`state == "PAUSE"`.

The firmware's `stg_cur` sub-stage distinguishes them (6 = filament ran out,
16 = paused by the user, plus a dozen fault codes). BambuBuddy parses it into
its printer state and then leaves it out of the status payload. It reaches
the web UI and parts of the REST API, but not the webhook status route polled
here. So the tile says `Paused` and can say nothing about why.

Also: BambuBuddy's plate detection can pause a print seconds after it starts
when it sees objects left on the bed. `PAUSE_SUPPRESS` ignores a pause for the
first 15 seconds so every print does not flicker through "Paused" on its way
to "Printing".

---

## What survives a crash

Everything above describes what happens while the process is running. This
section is about the moment it is not, which is where the failures are worst:
a tile stranded on someone's Lock Screen has nothing left that can update or
end it, and a duplicate tile burns one of five slots for eight hours.

**The start intent is written down BEFORE the request.** The obvious order,
POST and then persist the returned id, has a window: die inside it and the
gateway holds a tile nobody owns, and the next run cheerfully starts a second.
So `start_ambiguous` is set and committed first, and cleared only once the
gateway has answered one way or the other. A session that still carries the
flag at startup means the process died mid-start, and recovery lists the
device's tiles and adopts the orphan instead of creating another.

**Orphans are matched on the title as BUILT, not the printer name.** The
content builder collapses whitespace, strips control characters and truncates
by UTF-16 units, so comparing against the raw name would fail to match a
printer called `Office  X1C` against its own tile. Two unowned tiles with the
same title are left alone rather than guessed at: the title is the only
correlation the gateway offers and it is not unique.

**A failed end is not a success.** `DELETE` that times out leaves the session
in `ENDING` with its activity id intact, and `_retry_pending_ends` tries again
on the next tick. Ending is idempotent, so repeating it is free, and the
alternative is the stranded tile above. Rollover follows the same rule: if the
old tile could not be ended, the successor is not started, because two tiles
for one print is worse than a late rollover.

**Five flags are persisted because they decide what happens after a restart**:
`start_ambiguous`, `alert_sent`, `outcome_inferred`, `terminal_provisional`
and the linger time an outstanding end still owes. Leaving `alert_sent` in
memory meant a restart inside the terminal hold sent "Print finished" twice.
The schema migrates additively at open time, since `CREATE TABLE IF NOT
EXISTS` does nothing to a table that already exists.

**A tile-less session is not resumed, it is re-derived.** What the store
remembers is that a print WAS running when the process stopped, which may have
been days ago. Queuing it on that basis creates a tile for a print that
finished during the outage. Those sessions are retired and rebuilt only when a
live `RUNNING` or `PAUSE` reading confirms the print is real.

---

## A non-success outcome can be taken back

A NON-SUCCESS outcome declared while the newest reading in hand still says
`RUNNING` marks the session **provisional**. A provisional terminal is never
ended on a timer. Two things resolve it:

- a later `RUNNING` reading **revokes it outright** and the tile goes back to
  printing, or
- the printer stops confirming (silence, or a reading that is inactive), at
  which point the outcome was right after all and the tile ends.

A SUCCESS is never provisional: it outranks everything, so nothing arriving
later could improve on it.

This is a backstop rather than a busy code path, and it is worth being honest
about that. An outcome is inferred from the same state field the check reads,
so by the time there is an outcome the newest reading is usually the inactive
one that produced it. The flag costs a column in SQLite. The failure it was
written for cost a tile that had four hours left on it.

---

## Shutdown

Tiles are **left on screen**. A restart for a routine upgrade should not wipe
the Lock Screen of someone watching a print that is still running.

On the way back up, every stored session is reconciled against the gateway
before the first poll goes out:

- still live → adopt it and carry on, mid-countdown
- dismissed → record the dismissal so nothing recreates it
- reaped or gone → forget the tile; the normal path recreates one within a
  grace window if the print is genuinely still running

Recovery runs **before** the first request, so no reading can create a second
tile for a print that is mid-reattachment.

---

## If this ever goes native

The integration is organised around four operations, deliberately:

1. **Start** a print session
2. **Update** its current snapshot
3. **End** it with an outcome
4. **Recover** active sessions

A native BambuBuddy implementation would feed those same four from its
internal start/progress/complete/restart callbacks, and would have access to
things the status route does not carry: `subtask_id` as a real print id,
`stg_cur` for the pause cause, the layer counter, and the guarantee that a
start callback fires for every print. Roughly half the defensive machinery
here exists because those are missing from the routes a separate process can
reach.
