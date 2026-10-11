# Changelog

Notable changes, newest first. This project follows
[semantic versioning](https://semver.org/).

## 0.5.3

Every change here comes from reading the Notify gateway's own source and the
Notify app's architecture notes, rather than inferring their behaviour from
outside. Several things this service believed, and told its users, were wrong.

### Fixed

- **A segmented tile hid the reason a print stopped.** The widget's corner
  shows the countdown, then `trailing`, then the step fraction, then the status
  word. With `TILE_PROGRESS=segments` the fraction is always present, so a
  paused print read `4/10` instead of `Filament ran out`, a silent one `4/10`
  instead of `Connection lost`, and a failed one ended on `4/10`. The phase word
  now goes in `trailing` whenever there is no countdown.

- **`dead-token` was treated as the user swiping the tile away.** The gateway
  sets it only when the device's start credential is refused while a tile is
  still pending, which is to say no tile ever existed. The printer then got no
  tile for the rest of a print the user had never seen one for. It is now
  replaced like any other lost tile, and counted against the replacement limit.

- **A tile that might be appearing was thrown away.** A start that fails with
  a 502 marked `deliveryState: "unknown"` means Apple never answered, so the
  tile may still appear, and the gateway keeps its record and returns its id.
  That id was discarded, leaving the tile unowned for this service's own
  pre-start sweep to end later. It is now followed like any other start, with
  an expiry counted from the moment it began rather than from the age of the
  print, which would have rolled it over seconds after it started.

- **Other start failures now wait as long as the gateway asks.** A 502 that
  says nothing reached the phone carries `retryAfterSeconds` only when waiting
  helps, and is honoured. Without it, the gateway's own message (usually "open
  the Notify app once") is logged as an error. A 502 with no gateway body at
  all, which is a proxy in front of it, is treated as the passing outage it is.

- **Never-started tiles are replaced, at runtime and across a restart.** The
  earlier belief was that a reaped tile might be on screen and that nothing
  could ever remove it, so replacing it risked a permanent duplicate. Both
  halves were wrong. Push-to-start launches the Notify app in the background,
  where it reports the tile's token, so a tile that appears is normally
  confirmed within seconds and a reap almost always means it never appeared.
  If one did, the next app launch clears it. Declining therefore left prints
  with no tile, and on a restart it was worse: the reap was recorded as a
  dismissal, which also silenced every alert for that print and for a reprint
  of the same file within three days.

- **Failed alerts now say why.** Notify answers every delivery failure with
  the same "Failed to send notification" and puts the reason elsewhere in the
  response. A browser whose push subscription had been retired failed every
  alert, which on a browser is everything this service sends, and the log never
  said to reopen the Notify web app. Retired subscriptions and tokens are now
  logged as errors with the remedy; a passing Apple outage stays a warning.

- **The start throttle's advice is now Notify's own.** This service used to
  tell everyone that only opening the Notify app would clear it. Waiting clears
  it; opening the app can shorten it, but only when there is a tile or a pending
  start for the app to report. The gateway knows which and says so, and that is
  what gets logged now. A 429 that is not the gateway's throttle, from
  something in front of it, no longer sends anyone to the app at all.

- **The 90 second check no longer says something false.** It told users that
  an unconfirmed tile on screen meant nothing was wrong, because the app
  "only confirms tiles while it is open". It does not; see above.

### Documentation

- The README no longer says a browser may break through Do Not Disturb, which
  no browser allows, and now says a browser that is not running for more than
  about an hour drops alerts sent meanwhile. "Print started" is sent to a Mac as
  well as a browser.
- `ARCHITECTURE.md`, `TROUBLESHOOTING.md` and `CONFIGURATION.md` are corrected
  on every point above. `ARCHITECTURE.md` still said never-started meant
  "never recreate".
- `UPSTREAM.md` is marked as the historical record of the proposal that native
  Bambuddy support has since answered, with a note that some of its claims
  about the gateway were later corrected.

### Verification

Two adversarial reviews, the second of which found thirteen problems in the
first round of these fixes, including the restart path above. Seventeen
mutations across the fixes are all caught by the suite.

## 0.5.2

### Documentation

- **The README now opens by saying Bambuddy has Notify! built in.** Native
  Notify! support, with alerts, Live Activities and Lock Screen widgets, reached
  Bambuddy's dev branch on October 9, 2026. Anyone whose Bambuddy offers
  Notify! under Settings → Notifications is better served by that than by a
  separate container, and the first thing on the page now says so, with a link
  to the [setup guide](https://wiki.bambuddy.cool/features/notifications/#notify).

- **A new section, "Moving to Bambuddy's built-in Notify!",** gives the switch
  in the order that avoids two tiles per print: set up the built-in provider
  with its Live Activities off, remove this container, swipe away any tile it
  left behind, and only then turn Live Activities on. It also says why
  `ALERTS=off` is not enough on its own (it silences notifications, not
  tiles), and to turn off any generic webhook provider aimed at Notify!. This
  matches the migration note on Bambuddy's own wiki.

- **"For the BambuBuddy developer" is retired to a short note.** It asked for
  native support, which now exists; `docs/UPSTREAM.md` stays as the record of
  how it was proposed.

## 0.5.1

### Fixed

- **iPhones set up since Notify 6.09.02 were refused at startup** (#1). A fresh
  install of the Notify app now mints a 16-character device id, `IO` followed
  by 14 capital letters and digits, and this service recognised only the
  older 8-character shape for a phone. Everyone who had installed or
  reinstalled the app since then got "is not a Notify device id" and the
  container exited, with credentials the gateway was happy to accept.
  Reported, with a careful diagnosis, by [@do-gugan](https://github.com/do-gugan).

  All five of Notify's namespaces are now recognised, using the gateway's own
  patterns character for character:

  | Id | What it is | Tiles |
  |---|---|---|
  | 8 characters | iPhone or iPad, from before 6.09.02 | yes |
  | `IO` + 14 | iPhone or iPad, from 6.09.02 on | yes |
  | `WB` + 14 | browser subscription | no |
  | `MC` + 14 | Mac | no |
  | `GRP` + 5 | group | refused |

  The three 16-character namespaces are the same length, so they are told
  apart by prefix. The fix proposed in the issue widened the phone pattern to
  any 16 capital letters and digits, which would also have accepted a Mac id as
  able to host a tile. A legacy 8-character id that happens to start `IO`, `WB`
  or `MC` is still read as a legacy id, matching how the gateway's own register
  branches read it.

- **A Mac can run this now.** `MC` ids were refused outright. They now run in
  the same notifications-only mode as a browser, and the startup message says
  which of the two it is.

- **A browser id with a lowercase letter is refused here, with an explanation.**
  The gateway mints `WB` ids in capitals only, so a lowercase one can never be
  real. It used to pass this service's check and then fail at the gateway with
  a bare "not found". The README's example browser id had lowercase letters in
  it, which taught a format that cannot exist; it is fixed too.

## 0.5.0

### Added

- **`BAMBUDDY_RICH_STATUS`, and a paused print that says why.** BambuBuddy
  serves status on two routes and accepts the same `read_status` key on both:
  the advertised `/api/v1/webhook/` one reports seven fields, its own
  `/api/v1/printers/` one reports sixty-three. Polling the second is opt-in and
  gets HMS faults, layer counters, temperatures and the firmware's current
  stage.

  The headline is that a paused tile can now read **"Filament ran out"** rather
  than "Paused", which this integration has had to apologise for since the
  first release. It also revives `layer_text`, which had been unreachable dead
  code because the webhook route reports no layers.

- **`TILE_METRICS` takes a list**, so the chips are yours to choose:
  `progress`, `eta`, `layers`, `nozzle`, `bed`, `chamber`. `true` still means
  `progress,eta`. A temperature shows its target only while the printer is
  driving toward one, so an idle nozzle reads `24 C` and not `24/0 C`. A chip
  whose data this install cannot supply is dropped rather than shown as zero.

- **Four opt-in condition alerts:** `ALERT_HMS` (default `serious`, batching a
  multi-code event into one push), `ALERT_PAUSE`, `ALERT_PLATE_CLEAR` and
  `ALERT_OFFLINE`. The last needs no rich status. Every one is off by default,
  so upgrading pushes nothing new.

- **`TILE_STAGE`** puts the firmware's sub-stage ("Cleaning nozzle tip") in the
  status slot.

### Fixed, before any of it shipped

An adversarial review of the first cut found ten defects. Each now has a
regression test that fails when the fix is removed.

- **`ALERT_PLATE_CLEAR` could never fire.** The alert, the config and the
  documentation all existed, and the parser discarded `awaiting_plate_clear`.
  Its own test passed because the fixture set the field directly, bypassing the
  parser production uses.
- **A fallback announced a false all-clear.** The route is retired on refusal,
  after which every frame reports no faults. That was read as "the fault went
  away", so a user was told their filament runout was resolved while the spool
  was still empty. Each frame now records which route produced it, because the
  configuration flag still reads `true` while the frames are plain.
- **A 404 stopped the service.** An older BambuBuddy, or a proxy forwarding
  only `/api/v1/webhook/`, answers 404. That is a route being absent, not an
  outage, and treating it as one halted the poll loop at the first printer
  while the health endpoint still reported healthy.
- **An all-clear was sent for faults nobody was told about.** Suppressed faults
  are deliberately remembered so they are not rediscovered every poll, and the
  clear-side diff ran against that same set. On a printer with an AMS, routine
  advisories produced a stream of orphan "Fault cleared" pushes.
- **An advisory hijacked the pause word.** An AMS humidity warning can be
  active all print. It replaced "Paused" for a pause the user made themselves,
  and made it Time Sensitive. The tile now applies the same severity floor as
  the alerts.
- **Every restart re-announced a standing fault.** The fault set is now seeded
  from the frame that opens a session, so an active fault is known rather than
  discovered at 3am.
- **The new alerts ignored `ALERTS_TIME_SENSITIVE`**, overriding a Do Not
  Disturb preference the user had explicitly turned off.
- **`ALERTS=problems` silently ate the opt-in alerts.** Each is already an
  explicit request, so only `ALERTS=off` outranks one now.
- **A finished print announced its printer going offline**, which is what
  switching it off after taking the plate out looks like, and the matching
  "back online" could never arrive because the session is retired first.
- **The layer chip rendered `Layer 57/40`.** It repeated two of `layer_text`'s
  three checks and omitted the one that matters, since BambuBuddy holds the
  previous print's denominator until the firmware republishes.

### Notes

- Checked against BambuBuddy's source and a live instance. The route, its
  permission mapping (`Permission.PRINTERS_READ` to `can_read_status`) and the
  payload shape were all confirmed rather than assumed.
- Both parser fixtures in `tests/fixtures/` are real captured responses.
- Twenty mutations across the feature and its fixes are all caught.

## 0.4.2

### Documentation

- **The layout options read as if they replaced the default. They add to it.**
  Nothing in 0.4.0 or 0.4.1 removed the progress bar or the countdown, but the
  only worked examples given were ones that turned things off, and the metrics
  note said it "pairs well with `TILE_PROGRESS=off`", which reads as a
  requirement rather than a suggestion. Both the README and
  `docs/CONFIGURATION.md` now lead with a table of what each combination keeps,
  and the examples start from the default rather than from the minimal tile.

  The countdown is not a setting at all: it is sent in every layout whenever
  the printer is printing and has an estimate. That is now said outright.

- **Segments do not cost you the clock.** They compete for the same corner, and
  Notify fills it with the countdown first, then `trailing`, then the step
  fraction, then `status`. So a segmented tile shows the time while a print is
  running, and falls back to `3/4` only when there is no estimate. Verified
  against the published precedence rule rather than inferred from a screenshot,
  which had suggested the opposite. The same order is now correct in the
  `content.py` comment, which listed three of the four.

## 0.4.1

### Changed

- **Metric chips are two, not three.** The status chip is gone. Two chips fill
  the row on a phone, and the phase word already renders in the tile's trailing
  slot from the `status` field, which is sent on every payload whatever the
  layout. A third chip squeezed the two carrying numbers in order to repeat
  something already on screen.

### Documentation

- **The README said nothing about either new capability where anyone would
  look.** The tile layouts had a section, but "What it does" did not mention
  them, and the same was true of browser subscriptions. A reader forms their
  impression from that list, so both are now in it.

- **Browser subscriptions have a section of their own.** Support for them has
  existed for a while and was documented in one clause inside a prerequisites
  list, which read as though this were iOS only. There is now a "Without an
  iPhone" section covering what a `WB` device id does, that nothing needs
  configuring because the id's shape decides it, which platforms web push
  reaches, and a table of what changes. It also records the two startup
  refusals: a browser id with `ALERTS=off`, which would leave nothing to send,
  and a `GRP` group id, which is not a device.

- **The tile layouts are illustrated.** Five screenshots from Notify's own API
  reference now sit beside the settings that produce them, in the README and as
  a table in `docs/CONFIGURATION.md`. They are labelled as Notify's examples
  rather than this integration's output, because they are.

## 0.4.0

### Added

- **The other Live Activity layouts, as options.** Notify has no "tile type"
  field: the fields present in a payload ARE the layout, and they compose.
  These are therefore independent switches rather than a menu of presets, and
  every one of them defaults to exactly what this service already sent, so an
  existing deployment sees no change at all.

  - `TILE_PROGRESS` picks the progress indicator: `bar` (the smooth
    percentage bar, default), `segments` (discrete blocks via Notify's
    `steps`/`step`), or `off` (no bar, leaving the countdown and status word
    to carry the tile).
  - `TILE_SEGMENTS` sets how many blocks a segmented bar has, 2 to 20,
    default 10. A block lights only once its share of the print is genuinely
    finished. Rounding to nearest would fill the bar at 95% and leave a long
    print looking done for its last minutes.
  - `TILE_METRICS` swaps the model-name line for labelled chips: progress,
    time left, status. Off by default, because those chips take the place of
    the model name and that is what most people look at first. Progress is
    sent as `61%` rather than `61` with a unit, because a value ending in a
    percent sign earns its own miniature bar from the widget.
  - `TILE_BUTTON_URL`, `TILE_BUTTON_TITLE` and `TILE_BUTTON_OPEN` add an
    optional tappable button. The URL must be `https`, which is Notify's
    requirement, so a plain `http://` LAN address is rejected at startup
    rather than producing a button that quietly does nothing when tapped.

  Exactly one progress shape is ever sent, and the unused one is explicitly
  cleared rather than omitted. An omitted field keeps its previous value, so
  without that a settings change, or a tile adopted from an earlier run, would
  draw both bars at once for the rest of its life.

  Layout changes take effect on the next tile. A print already running keeps
  the layout it started with, so nothing rearranges itself mid-print.

### Documentation

- README gains "Choosing how the tile looks", which opens by saying none of it
  is necessary, and `docs/CONFIGURATION.md` and `.env.example` cover each
  option with the reasoning behind its default.

### Notes

- Checked against the published reference at
  https://getnotifyapp.com/apidocs/ rather than from memory.
- Each new test was verified by mutation: rounding segments to nearest,
  failing to clear the unused bar, leaving metrics set when off, splitting the
  percentage into value and unit, not filling the segments on a finished
  print, and resending the button on every update are all caught.

## 0.3.4

**Use this instead of 0.3.3.** That release rested on a safety property that
does not exist, and on a device whose Notify app stays closed it would have put
a second tile on the Lock Screen beside the frozen one.

### Fixed

- **A replacement tile is no longer created on evidence that cannot support
  it.** 0.3.3 replaced a tile the gateway had reaped as never-started, on the
  stated grounds that the pre-start sweep would first remove any tile that had
  actually arrived. It cannot. `_reap_stale_tiles` draws its candidates from
  `list_device`, which returns only LIVE tiles, and a reaped tile is by
  definition already ended in the gateway's own records. It can never appear
  there, and no request removes it. That claim was in the code comment, the
  commit message and this changelog, and it was wrong in all three.

  A never-started reap means the Notify app did not confirm the tile within
  about fifteen minutes. The app confirms only while it is open, so that
  covers both "iOS never displayed it" and "iOS is displaying it right now
  with the app shut", and the gateway cannot separate them either.

  So the question asked is now a different one: does this device confirm tiles
  at all? On a device that has confirmed at least one, a tile that went
  unconfirmed genuinely was not delivered and replacing it is safe. On a
  device that has never confirmed anything, non-confirmation says nothing, and
  the service leaves the screen alone and says so. Every other loss reason,
  where the gateway states it no longer holds the tile, is unambiguous and
  replaces as before.

- **An eight-hour rollover no longer spends the replacement budget.** The
  counter added in 0.3.3 was incremented in `_adopt`, the bind point for every
  tile a session is ever given, including the first start and each rollover.
  With a cap of two, any print crossing the rollover window had spent the
  allowance before anything went wrong, so the replacement was disabled on
  exactly the long prints that most need it. It now counts replacements only,
  and is named `recreate_attempts` to match.

- **A replacement tile is built from the current reading.** The branch ran
  before the frame was applied, so a tile replaced at 75% was born showing the
  previous reading's 40% and a stale countdown, which the progress floor then
  suppressed the correction to. Running it before new-print detection could
  also create a tile and supersede it inside one frame. It now runs after both.

- **`start_ambiguous` has its documentation back.** The 0.3.3 field was
  inserted between that comment and the field it describes.

### Notes

- Verified by upgrading a real 0.3.2 database: all 28 columns round-trip, the
  new column defaults correctly for an in-flight session, and a stored value
  reads back without displacing a neighbour.
- Each of the four new tests was checked by mutation: disabling the
  replacement, removing the never-confirmed guard, restoring 0.3.3's increment
  site, and removing the cap are all caught.

## 0.3.3

### Fixed

- **A print whose tile was reaped as never-started now gets another tile.**
  Since 0.2.2 this service declined to replace such a tile, because the gateway
  cannot distinguish a tile iOS never displayed from one it displayed while the
  app was closed, and replacing a tile that had in fact arrived produced two
  side by side.

  Declining was right. Leaving the session there was not. It stayed resident
  and non-terminal, which blocked the creation path, and its telemetry stayed
  fresh, which meant the silence sweep never retired it. The print then ran to
  completion with nothing on screen, while the tile already on the phone kept
  counting down on its own, because `endsIn` is relative and iOS ticks it
  locally with no further contact.

  A live print with no tile is now given another one on the next reading.
  Replacing is safe here because `_start_tile` already sweeps the device before
  every start, so a replacement cannot end up beside a tile that did arrive.
  Bounded at two per print by `MAX_TILE_ATTEMPTS`, tracked in the new
  `tile_attempts` column, because each start is spent against a device throttle
  only the phone's owner can replenish.

- **Repeated never-started reaps now say what actually needs doing.** Losing
  one tile is bad luck. Losing every tile is an unopened app, and the two
  needed different messages, because no amount of retrying fixes the second.

  The Notify app confirms tiles only while it is open. The gateway gives up on
  an unconfirmed tile after roughly fifteen minutes and counts it as a failed
  delivery, and its own refusal window climbs with each one: minutes, then
  hours. From the second reap onward this is logged at ERROR with the only
  remedy there is, which is to open the app once on the device.

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
