# Changelog

Notable changes, newest first. This project follows
[semantic versioning](https://semver.org/).

## 0.3.2

### Documentation

- **`docs/UPSTREAM.md` reads as an offer rather than a review.** The content
  was right and the register was wrong: it described difficulties as faults in
  BambuBuddy, quoted that project's own contributing rules back at its
  maintainer, and justified its size estimates in terms of this author's
  credibility.

  Every difficulty is now stated as something hit while building this, because
  that is what they were. All four are consequences of a Live Activity being
  stateful in a pipeline correctly built for stateless messages, not faults in
  anything. The process section is replaced by a statement of what will be done
  rather than a list of rules to avoid tripping over, and the close says
  plainly that adding four fields is a good outcome on its own.

## 0.3.1

### Documentation

- **`docs/UPSTREAM.md` is now an implementation guide**, not a list of
  requests. It offers two independent routes: four additive fields on the
  status route, or building Live Activities into BambuBuddy natively as a
  provider type.

  The native route is mapped against BambuBuddy's own Bark commit, which is
  the precedent for what a new provider costs, and pre-solves the four things
  that break when a resource-shaped feature is bolted to a message-shaped
  pipeline: no job identity reaching the sender, progress firing three times
  per print, quiet hours swallowing the end call and reporting success, and
  the write-lock rule that makes the obvious place to keep state the wrong
  one. Each has a diff rather than advice.

  It is deliberately honest about size: the provider type is an afternoon, the
  lifecycle behind it is a long weekend, and shipping the first without the
  second puts a tile on someone's Lock Screen that is never taken off.

## 0.3.0

Everything an independent review of the tile lifecycle turned up, plus what
verifying that review turned up in turn. A minor bump rather than a patch,
because the state store gains a column and new-print detection changes shape.

### Fixed

- **A swipe followed by a restart, or by a telemetry gap, silenced the printer
  for 72 hours.** A dismissal now records WHICH print it was made against, and
  is spent as soon as the printer reports a different file. Previously the
  only thing that could spend one was the session reaching a terminal state,
  and both a restart and a long gap destroy that session first, leaving
  nothing that could ever clear the row.

- **New-print detection could never fire.** It keyed on a layer count going
  backwards, and the REST route reports no layers at all, so the guard exited
  before its comparison on every frame. A second print silently rode the
  first one's session: no completion alert for the first, no start for the
  second, and the second inherited the first's age, which is what decides the
  eight hour rollover. It now keys on the file name changing or the
  percentage rewinding, both of which this API does carry, still behind the
  gap guard that keeps a multi-plate job as one print.

- **A dismissed session was never retired.** The abandon timer skipped it,
  because a dismissed session is not "live". It held the printer's only slot,
  which blocks tile creation entirely, and the one mechanism that could free
  it was switched off in exactly that state.

- **A retryable server error while ending a tile was treated as permanent.**
  The client already worked out that a 5xx is worth retrying and nothing read
  the flag, so a passing wobble abandoned the tile, live, with nothing left
  that could ever end it.

- **Restart recovery recreated a `never-started` tile**, reintroducing the
  duplicate-tile symptom 0.2.2 was cut to fix. The runtime path already
  refused this; recovery now applies the same rule.

- **The stale-tile sweep ended every ambiguous candidate.** Orphan adoption
  refuses to choose between two tiles claiming the same printer, and the sweep
  then ended both. Ending is more destructive than adopting, so the same doubt
  now stops it.

### Changed

- **The test fixture no longer describes hardware that cannot exist.** It
  defaulted to layer numbers the REST adapter never produces, so every
  coordinator test ran against an imaginary printer. That single default is
  why a whole detector sat unreachable under a green suite.

- **The state store migrates columns per table**, not just on `sessions`. A
  column added only to a `CREATE TABLE` exists for a fresh install and is
  missing for everyone already running the service, which is the exact
  population an upgrade is for.

### Documentation

- The operator docs described three behaviours that no longer exist:
  unconfirmed starts blocking new tiles, `never-started` being recreated, and
  an escalating local backoff. All three are corrected, with the reasoning
  that replaced them.

## 0.2.4

### Fixed

- **Swiping a finished tile away silenced that printer for 72 hours.** The
  worst defect this project has had, and it needed only the most ordinary
  gesture: the print ends, the tile says "Print finished", the user swipes it
  off the Lock Screen.

  A swipe means "not this print". It says nothing about the next one. But the
  terminal send is what discovers the swipe, by which point this print's
  dismissal has already been spent, so the dismissal recorded at that moment
  could never be spent by anything: session creation refuses before a session
  exists to reach a terminal state. Every subsequent print was silent for the
  full dismissal TTL, with no error, no log line, and a health endpoint
  showing a clean idle printer. `recover()` already carried the correct guard
  and explained the reasoning; the runtime path did not.

- **A recreated or rolled-over tile lost its countdown permanently.**
  `endsIn` is relative and is only resent once the printer's estimate drifts
  further than the tolerance from what is believed to be on screen. That
  belief was kept on the session rather than the tile, so a new activity was
  compared against a tile that no longer existed. Both estimates then decayed
  in lockstep, the tolerance was never crossed, and the successor never
  received a countdown at all: it kept its title, body, progress bar and
  status word, and silently lost the one field being read. Every rollover on
  a long print hit this.

## 0.2.3

### Fixed

- **An unconfirmed tile no longer blocks the next print.** This service was
  manufacturing its own outage, and it is the worst bug the project has had.

  A tile counts as "confirmed" only once the Notify app reports that iOS
  displayed it. For a background integration the app is normally closed, which
  is the entire point of push-to-start, so a tile can sit on the Lock Screen
  for hours and never be confirmed. This code read that as "iOS is discarding
  our tiles" and blocked new starts, escalating 15 to 60 to 180 minutes. Every
  blocked print then produced another unconfirmed start, escalating further,
  while the gateway applied its own throttle for the same reason. The two fed
  each other until a working phone went nearly two hours with no tiles, with
  the supposedly missing tile visible the whole time.

  An unconfirmed start now logs one warning that names both possibilities and
  does nothing else. The gateway's own `429` remains the only thing that
  blocks starts, since it is the only party that can actually see whether the
  device is receiving them.

- **An unconfirmed tile is no longer ended to reclaim its slot.** It may well
  be live, and removing a tile the user is looking at on the strength of a
  signal that cannot tell displayed from discarded is the wrong trade.

## 0.2.2

### Fixed

- **A stale tile is ended before a new one is started.** Losing the reference
  to a Live Activity does not remove it: iOS keeps one on the Lock Screen for
  up to eight hours on its own, so a tile whose id this service no longer
  holds sits there frozen while a replacement appears beside it. Every fresh
  start now sweeps the device and ends any tile that no session owns and whose
  title matches the one this printer would carry.

  Three ways to arrive with such a tile, all seen or reachable: the gateway
  reaping one it believed never appeared while iOS displayed it anyway, a
  start whose response was lost, and a state database thrown away with the
  container.

  The sweep skips tiles a live session owns, which matters because two
  printers with the same name produce indistinguishable titles and ownership
  is then the only thing stopping one from reaping the other's. It ends by
  `activityId` and never by device id, since that form ends "the device's
  single live tile" and would be a coin flip on a multi-printer setup.

### Documentation

- **The tile lifetime rules are written down** in `docs/ARCHITECTURE.md` and
  summarised for the BambuBuddy maintainer in `docs/UPSTREAM.md`: a tile
  outlives your memory of it, a swiped tile never returns, and ending is
  idempotent while not ending never is.

## 0.2.1

### Changed

- **One build per release, gated on the tests.** Publishing fired on both a
  branch push and a tag push, so one release was built twice and `latest` and
  the version tag ended up as different digests of the same commit. A version
  tag now names one exact artefact. The suite runs as a precondition of the
  build that ships, rather than trusting that a separate workflow passed on
  the same commit.
- **The documented image is a pinned release** rather than `latest`, so a pull
  cannot change what is running mid-print.

## 0.2.0

Everything here came from the first real prints on real hardware. None of it
could be reached by any test environment, because every one of these bugs
needed a printer that actually starts, stops and takes its time about it.

### Fixed

- **A finished print could leave a second tile on the phone.** The gateway
  reaps a tile it believes never reached the device, and that fell into the
  generic "recreate it, the print is still running" branch. But a tile iOS
  displays while the Notify app stays closed is never reported back, so it
  looks identical to one that never arrived. The replacement then sat
  alongside the original. That reap is now the one case never retried.
- **An adopted print showed an empty progress bar.** Progress is pinned to 0
  until a reading is seen to move, because BambuBuddy does not reset the field
  when a print starts. On this transport nothing announces a start, so every
  tile was treated as a fresh print and every print already under way showed
  nothing until the percentage happened to tick. A print this service watched
  BEGIN keeps the pin; one already running when it first looked does not.
- **The endgame ran at the idle poll cadence.** Readings dropped to one every
  80 seconds exactly when the coordinator starts timing a 90 second window,
  so a print took 160 seconds to be recognised as over. The fast cadence now
  holds for five minutes after a printer was last active.
- **A log line claimed the printer said RUNNING regardless of what it said.**

### Changed

- **The unconfirmed-start backoff is capped at 15, 15 then 30 minutes**, down
  from 15, 60 then 180. The signal it acts on cannot distinguish a discarded
  start from a displayed tile whose app never checked in, and three hours of
  no tiles on a working phone is too high a price for a guess.
- **The startup line now carries the build**: `notify-bambuddy 0.2.0 (build
  <commit>) is running`. A rolling `latest` tag makes a version number
  ambiguous, since two images months apart can report the same one. The
  commit cannot be.

## 0.1.0

First release. A proof of concept that is nonetheless meant to survive being
left running for a month.

### Added

- **A Live Activity per printer**, created when a print starts, updated in
  place while it runs, and ended with the right outcome when it finishes.
- **A live countdown** from the printer's own ETA. Because the device ticks it
  down locally, a whole print costs a handful of pushes rather than one every
  few minutes, no matter how often this service looks.
- **Pause and resume**, told apart and labelled.
- **Browser subscriptions are supported**, not just iPhones and iPads. A
  Live Activity is an iOS Lock Screen feature, so a `WB` device id gets no
  tile; it gets a notification when a print starts, finishes or fails
  instead, and every print is followed exactly the same way. Worked out from
  the shape of the device id, with nothing to configure.
- **Push notifications for the moments a tile cannot cover**: print finished,
  print failed, printer stopped responding. Problem alerts are sent Time
  Sensitive so they break through Do Not Disturb; successful completions never
  are, because a finished print at 3am is good news that can wait.
- **Multiple printers**, each with its own tile, keyed on a stable per-printer
  key so a rename in BambuBuddy changes nothing. Beyond the five-tile ceiling
  prints queue, and a freed slot goes to a print that is actually running
  before a paused one, then to whichever is closest to finishing.
- **Restart recovery.** Sessions are persisted and reconciled against the
  gateway on startup, so a restart reattaches to tiles still on screen rather
  than orphaning them or creating duplicates. Tiles are deliberately left on
  screen at shutdown: restarting for an upgrade should not wipe the Lock
  Screen of someone watching a print that is still running.
- **Dismissal is honoured** across restarts, scoped to the current print and
  cleared when a new one starts.
- **Privacy mode**, keeping model and file names off the Lock Screen.
- **A local health endpoint** reporting sessions, the tile budget and the poll
  counters, bound to localhost and carrying no credential.
- **`DRY_RUN`**, which tracks every printer and logs what it would send
  without creating, updating or ending a single tile.
- **Zero runtime dependencies.** A stock Python is the whole runtime, which is
  why the image is small and why there is nothing to audit.
- **A multi-architecture image**, so a Raspberry Pi or an ARM NAS works.

### Correctness work that is not a feature

Most of the code in this release exists because the obvious implementation is
wrong on real hardware. Each is pinned by a test named after the behaviour,
and explained in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md):

- `remaining_time` is converted from **minutes**, which is what BambuBuddy
  reports, despite documentation elsewhere reading unmistakably like seconds.
  Getting this wrong turns a two hour countdown into a two minute one.
- **Progress is not believed until it moves.** BambuBuddy never resets its
  progress field at print start, so the first readings of a new print carry
  the previous print's percentage. Without the gate a fresh tile appears at
  97% and jumps backwards.
- **The identity of every reading is cross-checked** against the printer that
  was asked about, rather than trusted from the response body, so a misrouted
  response cannot write one printer's progress into another's tile.
- **Readings are gated on their own age**, so a restart cannot act on
  something stale.
- **Terminal outcomes are held, not applied instantly**, so a later and better
  outcome can correct an earlier one before the tile ends.
- Every `410` is **classified by end reason** before deciding anything, so a
  gateway-reaped tile is recreated while a user dismissal is honoured.
- The five-tile cap arrives as a `400`, the one **transient** `400` in the
  API, so it queues rather than writing a printer off.
- Started tiles are **verified after 90 seconds**, because a start can be
  accepted everywhere and still silently produce nothing when the app's
  push-to-start credential has gone stale.
- Silence is derived from **when a reading last arrived**, not from the
  `connected` flag, which frequently never flips.
- The keepalive **bypasses the redundant-payload check**, because an unchanged
  tile is exactly the case where the gateway needs to hear from us.
- Both credentials are **redacted at the root logging handler**, so tracebacks
  raised from inside urllib cannot leak them either.
- **Backoff is a multiplier on the cadence actually in force**, so a
  BambuBuddy that is down is never polled harder than one that is merely
  quiet.

### Known limitations

- **Five live tiles per device.** That is the Notify gateway's own per-device
  cap, not this project's.
- **Eight hours per tile**, which is Apple's ceiling. Longer prints roll over
  to a successor at 7.5 hours.
- **No layer counter on the tile.** BambuBuddy's status route does not report
  layer numbers.
- **A pause has no stated cause.** The status route carries no error codes, so
  a filament runout and a deliberate pause are indistinguishable.
- **Terminal outcomes are inferred** from the printer's state going inactive
  rather than received as an event, so a print that both starts and finishes
  between two polls is never seen at all. `BAMBUDDY_POLL_SECONDS` bounds that
  window.
- **An immediate reprint of the identical file, started while this service was
  not running, is indistinguishable from the original.**
- **One destination device.** Group credentials cannot host a Live Activity.
- **Single replica.** Two copies pointed at the same phone would fight over
  the same tiles.

### Not yet verified

This has been exercised against a live BambuBuddy instance and against
replayed telemetry, but **not against a real printer running a real print with
a real phone**. The terminal-outcome path and the multi-hour rollover in
particular have not been seen end to end.
