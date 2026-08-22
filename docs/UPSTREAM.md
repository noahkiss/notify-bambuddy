# Live Activities in BambuBuddy

**For maziggy, or anyone picking this up.** This is a working iOS Live
Activity integration for BambuBuddy, running against a real X1 today, offered
as a starting point rather than as a request.

There are two ways to use it, and they are independent:

- **Add four fields to the status route.** Twenty minutes, no migration, and
  the external add-on stops guessing. [Jump there](#option-a-four-fields-twenty-minutes).
- **Build it in natively.** A new provider type, using the plumbing you
  already have. [Jump there](#option-b-native-a-provider-type-you-already-know-how-to-add).

Everything below is checked against `main` as of 2026-08-22, with file paths
and line numbers. Where something is hard, it says so.

---

## What it is

One Lock Screen tile per printer, created when a print starts, updating in
place, ending itself with the right outcome.

```
┌──────────────────────────────────────────────┐
│  🖨  Workshop H2C                   1:47:12  │
│     Articulated Dragon                       │
│     ████████████████░░░░░░░░░░░░░░░░  62%    │
└──────────────────────────────────────────────┘
```

The countdown ticks on the device, not from the server, so a whole print costs
a handful of pushes rather than one every few minutes. No unlocking, no
opening an app, no stack of "50% done" notifications.

Reference implementation: <https://github.com/simplytoast1/notify-bambuddy>.
AGPL-3.0, the same licence as BambuBuddy, so any of it can be lifted directly.

---

## Option A: four fields, twenty minutes

`printer_manager.get_status()` returns a `PrinterState` carrying four things
that never reach `PrinterStatusResponse` in
`backend/app/api/routes/webhook.py`. Adding them is additive, changes no
existing field, and breaks no existing consumer.

| Field | What it unlocks |
|---|---|
| `layer_num`, `total_layers` | A layer counter on the tile. The most requested thing after the countdown, and what tells you whether a first-layer problem is still recoverable. |
| `hms_errors` | Separates **"Paused"** from **"Filament runout"**. Without it a runout at 3am is indistinguishable from someone pressing pause, so the notification cannot say the one thing worth waking up for. |
| `subtask_id` | A per-print identity. Today a new print is inferred from a state transition between two polls, so a print that starts *and* finishes inside one interval is invisible and back-to-back prints of the same file are guesswork. |
| the printer serial | Sessions key on the database row id today, which survives a rename but cannot be cross-referenced with anything that identifies a printer by what the printer itself reports. |

### And one documentation fix that costs nothing

**`remaining_time` is MINUTES on the webhook status route and SECONDS in the
notification pipeline.**

`PrinterState.remaining_time` holds the firmware's `mc_remaining_time`
verbatim, which is minutes. The status route passes it through untouched.
`main.py` multiplies by 60 before `on_print_progress`, because
`_format_duration` expects seconds. Both are correct in context, and nothing
in the API reference says which is which.

Reading it as seconds turns a two hour countdown into a two minute one. It is
the most likely mistake any integrator will make, and one sentence next to the
field prevents all of it.

---

## Option B: native, a provider type you already know how to add

### Start with your own precedent

The last provider you added was Bark, commit `62ba751278`. The entire backend
was:

```
backend/app/schemas/notification.py            1 line   (the enum member)
backend/app/services/notification_service.py  46 lines  (the sender + 2 dispatch arms)
backend/tests/unit/services/test_notification_service.py  115 lines
CHANGELOG.md                                   1 line
```

No model change, no migration, no route change. That is the shape, and a Live
Activity provider fits it exactly:

1. `schemas/notification.py:22`, one member after `BARK = "bark"`.
2. `notification_service.py:293`, one `elif` in the test dispatch.
3. `notification_service.py:962`, one `elif` in the real dispatch.
4. One `_send_notify(self, config, title, message, ...) -> tuple[bool, str]`.
5. Frontend: `client.ts:2745`, `AddNotificationModal.tsx:15`, the locale files,
   and the `check-i18n-parity.mjs:128` brand alternation.

**That afternoon gets you push notifications through Notify.** It is a real,
shippable feature on its own, and it is where I would start.

### Now the honest part

A Live Activity is not a message. It is a **resource**: created once, updated
on a cadence, ended exactly once, rolled over at Apple's eight hour ceiling,
and reconciled after a restart.

Every `_send_*` in your codebase is fire-and-forget: `(config, title,
message)` in, one HTTP call, `tuple[bool, str]` out, nothing kept. The
resemblance is close enough to fool anyone for about a day.

**If you ship the afternoon version without the rest, you ship a feature that
puts a tile on someone's Lock Screen and never takes it off.** iOS leaves it
there for up to eight hours. That is worse than not shipping.

So the rest of this page is the four things that break, each with the fix,
because I hit every one of them.

---

### Wall 1: no job identity reaches the sender

`_send_to_provider` (line 919) receives `provider, title, message, db,
image_data, event_type, variables`. The only identity in there is
`variables["printer"]`, a display name a user can rename mid-print.

`printer_id` exists one frame up in `_send_to_providers` and is dropped at the
call site on line 1069. So the end call cannot find the activity the start
call created.

```python
# notification_service.py:919, add one parameter
     async def _send_to_provider(
         self, provider, title, message, db=None, image_data=None,
-        event_type=None, variables=None,
+        event_type=None, variables=None, printer_id: int | None = None,
     ):

# notification_service.py:1069, forward it
-    await self._send_to_provider(provider, title, message, db, image_data=..., ...)
+    await self._send_to_provider(provider, title, message, db, image_data=...,
+                                 printer_id=printer_id, ...)
```

A row id does not rename. A display name does.

### Wall 2: progress fires three times, and there is no pause event

`main.py:1599-1646` gates progress on 25/50/75 milestones, tracked in
`_last_progress_milestone`. There is no pause event at all.

Three updates across a print is not a Live Activity. Worse, the gateway reaps
a tile that has gone quiet for two hours, so a six hour print produces a tile
that dies at hour two and a phone that is now lying to its owner.

You already have the pattern for the fix: `start_digest_scheduler` /
`_digest_scheduler_loop` at `notification_service.py:2365-2387`, wired at
`main.py:8101` and `8176`. A keepalive loop in that shape, ticking every
30 to 60 seconds over rows with a live activity id, is roughly 80 lines.

### Wall 3: quiet hours swallows the end call and reports success

```python
# notification_service.py:931-933
if self._is_in_quiet_hours(provider):
    logger.info(...)
    return True, "Skipped - quiet hours"
```

Start a print at 21:00 with quiet hours from 22:00, finish at 23:00, and the
end is skipped and reported as success. The tile dangles.

**An end is not a notification. It is a resource release.** It has to bypass
quiet hours, and so does the keepalive.

There is a second version of this: `on_print_start` defaults to **False**
(`models/notification.py:63`, mirrored at `AddNotificationModal.tsx:33`) while
the completion events default True. The default configuration therefore
produces ends with no starts. For this provider type the start event is not
optional, and the UI should say so.

### Wall 4: you must not write state from the send path

Issue #2770 taught this codebase that a network call made while a caller holds
a session can lock SQLite for the whole connect timeout. The docstrings at
`notification_service.py:128-135` and `984-1016` spell it out.

So the obvious place to keep the activity id, `provider.config`, is exactly
the thing not to touch there.

The cheap correct route is a **new table**, which needs no hand-written
migration because `Base.metadata.create_all` at `database.py:310` runs first
on both dialects. Model it on `active_print_session.py`:

```python
# backend/app/models/notification_live_activity.py  (new file)
class NotificationLiveActivity(Base):
    __tablename__ = "notification_live_activities"

    id = Column(Integer, primary_key=True)
    provider_id = Column(Integer, ForeignKey("notification_providers.id"),
                         nullable=False, index=True)
    printer_id = Column(Integer, nullable=False, index=True)
    activity_id = Column(String(64), nullable=False)
    subtask_id = Column(String(64))      # which print this tile belongs to
    started_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime)        # Apple's 8 hour ceiling
```

Then one import line in the `from backend.app.models import (...)` block
inside `init_db()` at `database.py:253`.

Adding columns to `notification_providers` instead is the expensive route: a
dual-dialect entry in the hand-written `run_migrations` at `database.py:1242`,
inside a 5,023 line file, plus a migration test to match the twenty that
already exist. There is no Alembic here.

---

## The gateway contract

The whole API is at <https://getnotifyapp.com/apidocs/>. These are the parts
that cost me time, offered so they cost you none. Every one of them was
learned by getting it wrong against a real printer.

- **`endsIn` is RELATIVE, in seconds.** Send it once and iOS counts down on
  the device. This is why the feature is cheap. It also means resending the
  same value a few seconds later silently pushes the finish time out, so only
  send it when the printer's own estimate has actually moved.
- **`new=1` on start, always.** Without it the device address upserts, which
  updates a *different* printer's tile when exactly one is live and returns
  `409` when two or more are. With several printers that is the normal state.
- **`410` means seven different things.** `GET /live-activity/{id}` returns
  `endReason` as a clean field. `dismissed` and `dead-token` mean never
  recreate for this print. `overdue`, `abandoned` and `expired` mean recreate.
  `never-started` means the gateway believes no tile appeared, and **you must
  not recreate on it**: a tile iOS displays while the app is closed is never
  reported back either, so the two are indistinguishable, and recreating puts
  a second tile beside a live one.
- **A tile outlives your memory of it.** Up to eight hours, whether or not you
  still hold its id. Persist the id, and end any old tile before starting a
  new one for the same printer. Ending is idempotent; not ending never is.
- **A swipe means "not this print", never "not the next one".** Recording a
  dismissal discovered *after* a print already finished silenced a printer for
  three days in my implementation before I caught it.
- **Unconfirmed is not failed.** A tile is confirmed only when the app reports
  iOS displayed it, and for a background integration the app is normally
  closed. A healthy tile is routinely unconfirmed. Treating that as failure
  and backing off cost me a two hour self-inflicted outage.
- **Five live tiles per device**, arriving as a transient `400`. A sixth
  printer queues rather than failing.
- **`429` is a push-to-start throttle** that only the user opening the Notify
  app clears. Waiting does not help.
- **The app must have been opened once** since installing a Live Activity
  capable build. This is the single most common support question and is
  invisible server-side: iOS accepts the push and discards it silently.
- **`progress` is not reset at print start.** The first frames of a new print
  carry the previous print's percentage, so a tile appears at 97% and jumps
  back to 2%. Inside BambuBuddy you know when a print begins, so you can
  simply not believe the first reading; outside, it takes a heuristic.

---

## What you get to delete

Most of the reference implementation exists **only because it is outside**.
Roughly half of its 1,900 line coordinator is inference that BambuBuddy does
not need:

| Outside | Why | Inside BambuBuddy |
|---|---|---|
| `_detect_new_print` | Nothing announces a start | You fire the event |
| `progress_trustworthy` | First readings carry the last print's % | You know the print is new |
| `_detect_silence`, inactive grace | Nothing announces an end | You fire the event |
| `running_grace` | A flapping poll is not a print | No polling |
| `_adopt_orphan`, title matching | No print id exists | You have `subtask_id` |
| `terminal_provisional` | An inferred outcome may be wrong | The outcome is known |
| poll cadence, backoff, freshness gates | Polling | Event driven |

Six of my last eight releases were lifecycle bugs caused by not knowing things
your code knows for free.

---

## Honest sizing

Two numbers, because conflating them is how this loses your trust on day two.

**The provider type**, matching the Bark commit: an afternoon. 22 files, ~271
lines, of which 46 are the sender and 166 are tests. Ships working push
notifications.

**The Live Activity behind it**: two to four focused days for someone fluent
in this codebase. Roughly 700 to 1,200 lines of backend plus tests. A gateway
client with typed error classification is ~250 to 350; the lifecycle state
machine (persist, reconcile on restart, roll over at eight hours, keepalive,
end exactly once) is ~250 to 400; the keepalive loop ~80.

Add a day for the wiki PR and review.

### Process, so a PR does not bounce on it

Your `CONTRIBUTING.md` is explicit and easy to miss from outside:

- **No assigned issue, no PR.** Open an issue and get it assigned first.
- A **companion PR in `maziggy/bambuddy-wiki`** is mandatory for a new feature
  and for a new config key.
- The `static/` bundle is rebuilt as part of the commit, not hand-edited.
- All 13 locale files, plus the parity script's brand alternation.

---

## Contact

Open an issue on <https://github.com/simplytoast1/notify-bambuddy>, or comment
on any BambuBuddy issue that references it.

Happy to send the PR rather than just describing it, for either option. Both
projects are AGPL-3.0, so anything here can be lifted directly with no licence
question to resolve.
