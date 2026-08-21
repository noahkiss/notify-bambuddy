# Notes for the BambuBuddy developer

This add-on is built entirely on BambuBuddy's public surface and needs no
change to BambuBuddy to work. This page exists because a few small, additive
changes would remove every remaining limitation, and because everything asked
for below is a field BambuBuddy already has in memory.

Nothing here is a bug report. Nothing here blocks anything.

---

## What it does today

It creates an iOS **Live Activity** for each running print: a Lock Screen tile
with the printer, the job name, a progress bar and a live countdown, which
ends itself with the right outcome when the print does. The countdown is
relative, so iOS ticks it down on the device and a whole print costs a handful
of pushes rather than one per update.

It reads BambuBuddy through two `GET` routes, with an API key carrying only
`read_status`:

```
GET /api/v1/webhook/queue                 -> which printers exist
GET /api/v1/webhook/printer/{id}/status   -> state, progress, ETA, job name
```

It writes nothing, controls nothing, and never connects to a printer. If it
dies, BambuBuddy does not notice.

Every reading is answered live and stamped on arrival, so a reading's age is
the round trip that fetched it.

---

## The gap, stated plainly

`PrinterStatusResponse` currently returns:

```json
{"id": 1, "name": "Workshop H2C", "connected": true, "state": "RUNNING",
 "current_print": "Articulated Octopus", "progress": 52.0, "remaining_time": 43}
```

`printer_manager.get_status()` returns a `PrinterState` that also already
holds `layer_num`, `total_layers`, `hms_errors` and `subtask_id`. None of the
four reaches the status route, and each one closes a gap set out below.

## What would help, in priority order

Each of these is additive. None changes an existing field, so none breaks an
existing consumer.

### 1. `layer_num` and `total_layers` on the status response

Already on `PrinterState`. Two lines in `webhook_get_printer_status`.

Gives the tile a layer counter, which is the single most requested thing on a
print notification after the countdown, and is what a user looks at to decide
whether a first-layer problem is still recoverable.

### 2. `hms_errors` on the status response

Already on `PrinterState`. This is what separates **"Paused"** from
**"Filament runout"**. Without it a runout at 3am is indistinguishable from
someone pressing pause, so the notification cannot say the one thing that
would get somebody out of bed.

### 3. `subtask_id` on the status response

Already on `PrinterState`. Today a new print is inferred from a state
transition between two polls, which means a print that starts *and* finishes
inside one poll interval is invisible, and back-to-back prints of the same
file are hard to tell apart.

An explicit per-job id makes that exact rather than inferred, and it is the
one field here that removes a whole class of edge case rather than adding a
feature.

### 4. An all-printers status route

The published API reference lists:

```
GET /api/v1/webhook/status  -- Get all printer status
```

but it returns `404` on current builds; the per-printer route is the only one
that exists. A list form turns N requests per poll into one. With four
printers at a ten second interval that is 24 requests a minute rather than 6.

Not urgent, and easy to work around, but it is already documented as existing.

### 5. The printer serial on the status response

The API identifies printers only by BambuBuddy's database id, so this add-on
keys its sessions as `bb1`, `bb2`. That is stable in practice, and it survives
a rename, but it is stable to the *database row* rather than to the *printer*,
and it cannot be cross-referenced with anything else that identifies a printer
by the serial the printer itself reports.

### 6. One documentation fix, which costs nothing

**`remaining_time` is in MINUTES on this route, and in SECONDS in the
notification pipeline.**

`PrinterState.remaining_time` holds the firmware's `mc_remaining_time`
verbatim, which is minutes, and the webhook status route passes it through
untouched. `main.py` multiplies by 60 before calling `on_print_progress`,
because `_format_duration` expects seconds. Both are correct in context, and
nothing in the API reference says which is which.

Getting this wrong turns a two hour countdown into a two minute one, and it is
the most likely mistake any integrator will make. One sentence in the API
reference next to the field prevents all of it.

---

## The webhook notification provider, and why it is not used here

The generic **webhook** provider is the obvious integration point, and it is
what some other add-ons use. It is not used here, for two specific reasons
rather than any general objection:

- `on_print_progress` fires only at the **25%, 50% and 75%** milestones, so a
  progress bar driven by it moves three times per print.
- The payload reports time remaining as `_format_duration(...)` output, a
  preformatted string such as `"2h 14m"`. A Live Activity needs a real number
  of seconds, so a consumer has to parse that string back into one, and a
  string built for humans is not a stable interface.

If those two changed, a webhook receiver would be a better design than polling
for this: it is push rather than poll, and it needs no API key. Specifically,
a numeric field alongside the formatted one, say `remaining_seconds`, plus a
progress event that is not restricted to three milestones, and this add-on
would be a webhook receiver instead. The rest of the payload is already
well-shaped.

---

## If you would rather build it in

The Live Activity side is smaller than it looks, and the whole contract is at
<https://getnotifyapp.com/apidocs/>. The parts that took the longest to get
right here, offered so nobody has to rediscover them:

- **`endsIn` is relative, in seconds.** Send it once and iOS counts down on
  the device. This is why the whole feature is cheap: it does not need an
  update to keep the timer honest.
- **`new=1` on start, always.** Without it the device address upserts, which
  silently updates a *different* printer's tile when exactly one is live and
  returns a `409` when two or more are. With several printers that is the
  normal state, not an edge case.
- **Five live tiles per device**, enforced by the gateway with a `400`. A
  sixth printer has to queue for a slot.
- **Eight hours is Apple's ceiling** on a Live Activity. Longer prints need
  the tile rolled over.
- **The app must have been opened once** since installing a Live Activity
  capable build, or every start is refused. This is the single most common
  support question, and it is invisible from the server side: iOS accepts the
  push and discards it silently, so nothing reports an error.
- **`410` means seven different things.** The status endpoint returns
  `endReason` as a clean field, which is the only way to tell "the user swiped
  it away" from "the gateway tidied up a tile whose print is still running".
  Those two need opposite responses.
- **Progress cannot be believed at print start.** BambuBuddy does not reset
  `progress` when a print begins, so the first frames of a new print carry the
  previous print's percentage. A tile that appears at 97% and then jumps back
  to 2% is the result. This add-on pins progress to 0 until the reading has
  demonstrably moved.

The implementation here is AGPL-3.0. If a different licence would make reuse
easier, ask: the point of writing it was to make the feature exist, not to own
it.

---

## Contact

Open an issue on this add-on's repository, or comment on any BambuBuddy issue
that references it. Happy to send a pull request for any of the field
additions above rather than just asking for them.
