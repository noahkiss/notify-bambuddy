# Changelog

Notable changes, newest first. This project follows
[semantic versioning](https://semver.org/).

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
