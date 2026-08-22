# Live Activities in BambuBuddy

**For maziggy, or anyone who picks this up.** This is a working iOS Live
Activity integration for BambuBuddy, running against a real X1 today. It needs
nothing from you to keep working, so none of this is a request. It is here
because everything I learned building it is more useful to you than to me.

Two independent options, and either one is a good outcome:

- **Add four fields to the status route.** Twenty minutes, no migration, and
  the external add-on stops guessing. [Jump there](#option-a-four-fields-twenty-minutes).
- **Build it in natively.** A new provider type, using the plumbing you
  already have. [Jump there](#option-b-native-a-provider-type-you-already-know-how-to-add).

Everything below is checked against `main` as of 2026-08-22, with file paths
and line numbers, so nothing here should send you hunting.

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

### The part I got wrong first

A Live Activity looks like a notification and behaves like a **resource**:
created once, updated on a cadence, ended exactly once, rolled over at
Apple's eight hour ceiling, and reconciled after a restart.

I built mine as though it were a message, and it took me a while to notice.
The shapes are close enough that the difference does not show up until a print
ends.

The thing I would most like to save you: **a tile that is never ended stays on
the Lock Screen for up to eight hours.** So the end call matters more than the
start one, and it is the call most easily lost.

What follows is the four places that caught me out, each with what I did about
it. None of them is a problem with BambuBuddy. They are all consequences of
Live Activities being a stateful thing in a pipeline built, correctly, for
stateless ones.

---

### 1. The sender cannot tell which print it is for

`_send_to_provider` (line 919) receives `provider, title, message, db,
image_data, event_type, variables`. The only identity in there is
`variables["printer"]`, which is a display name.

`printer_id` is right there one frame up in `_send_to_providers` and simply
is not forwarded at line 1069, because nothing has needed it until now. Two
lines:

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

### 2. Progress fires three times per print

`main.py:1599-1646` gates progress on 25/50/75 milestones, tracked in
`_last_progress_milestone`. There is no pause event at all.

Perfect for a notification, and not quite enough for a tile: the gateway
reaps one that has gone quiet for two hours, so a six hour print would show a
tile that stops updating at hour two.

The good news is you already have the exact pattern for this.
`start_digest_scheduler` / `_digest_scheduler_loop`
(`notification_service.py:2365-2387`, wired at `main.py:8101` and `8176`) is
the shape. A keepalive in that mould, ticking every 30 to 60 seconds over rows
that hold a live activity id, came to about 80 lines for me.

### 3. Quiet hours also silences the end call

```python
# notification_service.py:931-933
if self._is_in_quiet_hours(provider):
    logger.info(...)
    return True, "Skipped - quiet hours"
```

Exactly right for a push, which is what it was written for. For a tile it
means a print started at 21:00 and finished at 23:00 leaves its tile on screen,
with the send reported as successful.

The distinction that helped me: an end is not a notification, it is releasing
something. Quiet hours should still silence the start and the alerts, and let
the end and the keepalive through.

There is a gentler version of the same thing worth knowing about:
`on_print_start` defaults to **False** (`models/notification.py:63`, mirrored
at `AddNotificationModal.tsx:33`) while the completion events default True.
Sensible for notifications, but for this provider type it would produce ends
with no starts, so the start event wants to be on by default here, or the UI
wants to say why.

### 4. The activity id needs somewhere to live

The obvious place is `provider.config`, and the docstrings at
`notification_service.py:128-135` and `984-1016` talked me out of it: a
network call made while a caller holds a session can hold the SQLite write
lock for the whole connect timeout. That is a lesson already learned here and
I did not want to undo it.

A **new table** avoids the question entirely, and needs no hand-written
migration because `Base.metadata.create_all` at `database.py:310` runs before
`run_migrations` on both dialects. Modelled on `active_print_session.py`:

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

Adding columns to `notification_providers` instead would mean a dual-dialect
entry in `run_migrations` at `database.py:1242` plus a matching migration
test, which is a lot more moving parts for the same result.

---

## What the Live Activity API taught me the hard way

The whole API is documented at <https://getnotifyapp.com/apidocs/> and is
genuinely good. These are just the things I only understood after getting them
wrong against a real printer, collected so they cost you nothing.

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
  `never-started` is the one that caught me: it means the gateway believes no
  tile appeared, but a tile iOS displays while the app is closed is never
  reported back either, so the two look identical from the server. I recreated
  on it and got two tiles side by side on a real phone. Best treated as "leave
  it alone".
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

This is the part that would make it fun to build. Most of my implementation
exists **only because it is outside**, guessing at things you simply know.
Roughly half of its 1,900 line coordinator would just evaporate:

| Outside | Why | Inside BambuBuddy |
|---|---|---|
| `_detect_new_print` | Nothing announces a start | You fire the event |
| `progress_trustworthy` | First readings carry the last print's % | You know the print is new |
| `_detect_silence`, inactive grace | Nothing announces an end | You fire the event |
| `running_grace` | A flapping poll is not a print | No polling |
| `_adopt_orphan`, title matching | No print id exists | You have `subtask_id` |
| `terminal_provisional` | An inferred outcome may be wrong | The outcome is known |
| poll cadence, backoff, freshness gates | Polling | Event driven |

Six of my last eight releases were lifecycle bugs caused by inferring
something your code already has sitting in a variable. Inside BambuBuddy, most
of that difficulty is not solved so much as absent.

---

## How much work is it, roughly

Two numbers, because they are quite different jobs and I would rather you know
that from me than find out on the second day.

**The provider type**, following the Bark commit exactly: an afternoon. Around
270 lines, of which 46 are the sender and 166 are tests. On its own that ships
Notify push notifications, which is a real feature, and it is where I would
start if it were my repo.

**The Live Activity behind it**: two to four focused days for someone who
knows this codebase, which is to say considerably less for you than it was for
me. Roughly 700 to 1,200 lines including tests. The gateway client with its
error classification came to about 300, the lifecycle state machine about 350,
the keepalive about 80.

If any of that sounds like more than it is worth, **option A is a genuinely
good outcome on its own.** Four fields, twenty minutes, and the add-on stops
guessing. I would be glad of that and would not raise it again.

---

## If it would help

I am happy to do the work rather than describe it, for either option, in
whatever form suits you: a PR against `bambuddy`, a branch to look at, or just
answering questions while you write it yourself. I will follow your process,
open an issue and wait for it to be assigned first, and do the wiki PR
alongside.

If you would rather not take a dependency on Notify specifically, the same
shape works for any Live Activity provider. The lifecycle is the hard part and
it is provider agnostic.

Both projects are AGPL-3.0, so anything here can be lifted directly, with or
without me.

**Reference implementation:** <https://github.com/simplytoast1/notify-bambuddy>

Thanks for BambuBuddy. The reason this add-on exists at all is that the API
was good enough to build on without asking anyone's permission, which is not
the normal experience.
