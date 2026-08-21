# Troubleshooting

Start here, always:

```bash
curl -s localhost:9337/status | python3 -m json.tool
```

That output contains no token and no API key. It does include `url`, which is
whatever you set `BAMBUDDY_URL` to, so redact that one line before pasting it
into an issue if the address is not one you are happy to publish.

---

## Nothing happens at all

Look at `source` in the status output. It holds everything the service knows
about its conversation with BambuBuddy:

```json
"source": {
  "source": "api",
  "url": "http://192.168.1.10:8000",
  "printers_known": {"1": "Workshop H2C", "2": "Office X2D"},
  "printers_polled": [1, 2],
  "polls_succeeded": 431,
  "events_emitted": 12,
  "consecutive_failures": 0,
  "last_error": null,
  "seconds_since_success": 3.1,
  "poll_interval": 10.0
}
```

`polls_succeeded` at 0 means nothing has ever been read, and `last_error` says
what stopped it. Every answer it can give has a section below.
`polls_succeeded` climbing while `events_emitted` stays at 0 means BambuBuddy
is answering fine and nothing is printing.

---

## Nothing arrives from BambuBuddy

### `BambuBuddy rejected the API key`

A `401` or a `403`, and at startup it is fatal: the service exits with code 2
instead of retrying, because a key that is wrong now will still be wrong in
ten seconds. Under a supervisor or in Docker that shows up as a restart loop
printing the same line every time.

The message carries BambuBuddy's own `detail` text, so BambuBuddy tells you
what it objected to in its own words:

```
BambuBuddy rejected the API key (HTTP 403: <BambuBuddy's own "detail" text>). Check BAMBUDDY_API_KEY against Settings -> API Keys, and that the key carries the 'read_status' permission for the printers you want.
```

A `401` means the key was not accepted at all: deleted, mistyped, or
truncated on paste. A `403` means it was accepted and then not allowed to do
this, which for the two endpoints used here means it is missing the
`read_status` permission. That is the only permission this service needs, and
it needs nothing that writes.

The check happens on the first call, the printer discovery against
`/api/v1/webhook/queue`, so it fails before any tile can exist. If you pass
the key by file, check `BAMBUDDY_API_KEY_FILE` points at a file holding the
key and nothing else.

The key travels in the `X-API-Key` header, never in a URL, and it is redacted
from the logs, so you will not find a copy in either place to compare
against.

### The JSON parse error names `BAMBUDDY_URL`

It names it deliberately: when BambuBuddy's reply is not JSON, the URL is
nearly always the problem rather than BambuBuddy. Something else answered.
The usual something else is an SSO or auth proxy in front of BambuBuddy
returning its own HTML login page, which is a perfectly successful `200` as
far as HTTP is concerned and complete nonsense as far as this service is
concerned.

`BAMBUDDY_URL` wants the base URL that serves `/api/v1/...` directly. Confirm
with the curl below: if what comes back starts with `<!DOCTYPE html>`, you
have found it.

### BambuBuddy is unreachable

Connection refused, a DNS failure, or a timeout (`BAMBUDDY_TIMEOUT`, 10
seconds by default). None of these are fatal and none of them stop the
service. `last_error` holds the text, `consecutive_failures` counts them, and
`seconds_since_success` grows. `/readyz` goes unready: ready means the last
poll succeeded and at least one poll ever has.

While it is failing, the poll backs off by doubling the interval it would be
using anyway, capped at 120 seconds, and never polls faster than the healthy
cadence. That last clause matters more than it sounds: without it, a broken
BambuBuddy gets hit harder than a quiet one.

A `poll_interval` that is not the number you configured is usually not a
fault. It is `BAMBUDDY_POLL_SECONDS` (10 by default, 2 minimum, 300 maximum)
while anything is printing, and eight times slower with a 30 second floor
when every printer is idle. A configured 10 reading as 80 means nothing is
printing.

### Some printers never appear

`printers_known` comes from the queue endpoint, and that endpoint only lists
printers the API key is scoped to. A printer missing from `printers_known` is
a printer the key cannot see, and no amount of configuration here will
conjure it up: issue a key that covers it.

A printer that is in `printers_known` but not counted in `printers_polled` is
being filtered instead. Check `BAMBUDDY_PRINTER_IDS`, which takes the plain
ids as the queue endpoint reports them (`1,2`) and means all printers when
empty, and check `PRINTER_ALLOWLIST`, which matches session keys (`bb1`)
rather than ids and is covered under "Only some printers get tiles" below.

A printer added in BambuBuddy after startup appears at the next discovery
pass, which runs every `BAMBUDDY_DISCOVERY_SECONDS` (300 by default). Allow
five minutes before concluding anything.

### Checking by hand

Both endpoints are `GET` and both need only `read_status`, so this is safe to
run against a live machine:

```bash
curl -s -H "X-API-Key: bb_..." http://HOST:8000/api/v1/webhook/queue
```

That is the same call the service makes at startup. A `401` or `403` here is
your answer. Otherwise it lists the printers, and their `printer_id` values
are exactly what `BAMBUDDY_PRINTER_IDS` takes.

Then one printer's telemetry:

```bash
curl -s -H "X-API-Key: bb_..." http://HOST:8000/api/v1/webhook/printer/1/status
```

```json
{"id":1,"name":"Workshop H2C","connected":true,"state":"RUNNING",
 "current_print":"Articulated Octopus","progress":52.0,"remaining_time":43}
```

That is a real captured response. `remaining_time` is 43 there and it means
43 minutes, not seconds: BambuBuddy stores the firmware's `mc_remaining_time`
verbatim and this route passes it through untouched.

There is no printer serial anywhere in that response, which is why sessions
are keyed by `bb` plus BambuBuddy's own printer id. Printer 1 is `bb1`, and
it stays `bb1` when you rename the printer.

The key is a header rather than a query parameter, so it stays out of URLs
and out of logs. It does land in your shell history, which is worth clearing
if that machine is shared.

---

## The log says the tile started, but nothing appeared

This is almost always one thing, and after 90 seconds the service says so:

```
Tile LA7Q2ZKM for bb1 was accepted but has not appeared on the phone. This
usually means iOS is discarding Live Activity starts because Notify's start
credential is stale. Open the Notify app once on the device.
```

**Open the Notify app on the phone, once.** That is the whole fix.

While that is outstanding the service deliberately does NOT end the pending
tile and try again. Each retry would be another push iOS discards, which keeps
Apple's throttle shut, and the gateway itself re-sends any start still inside
its fifteen-minute window the moment the phone reports a fresh credential. So
the tile is left for the gateway to finish, further starts are held back for
15 minutes (then an hour, then three), and the only time a stuck tile is ended
early is when another print is waiting for its slot.

The reason is worth knowing because nothing else in the stack reports it. iOS
lets an app create a Live Activity while it is closed using a credential
registered at launch. Updating or reinstalling the app rotates that
credential, and pushes sent to the old one are accepted by Apple and then
silently discarded. No error reaches the gateway, the app, or you.

The gateway defends the device's spawn budget: after two confirmed losses it
refuses further starts with a `429` and an escalating quiet period (30
minutes, then 3 hours, then 6). Waiting alone does **not** clear it. The
counter resets when the phone reports a fresh credential, which happens when
you open the app.

If that has already happened, the status output tells you how long is left:

```json
"starts_blocked_for": 1620.0,
"starts_blocked_reason": "Notify is refusing new tiles for this device because
  recent ones never appeared. Open the Notify app once on the phone..."
```

Open the app, then wait out the stated time.

---

## `The Notify device cannot show Live Activities`

A `409` from the gateway, which means it knows exactly why and the message
says which of two things it is:

- **No start credential yet.** The app has never been opened since installing
  a version that supports Live Activities. Open it once.
- **Live Activities are turned off.** In iOS Settings → Notify, or globally in
  Settings → Face ID & Passcode → Live Activities.

Both are fixable only on the phone.

---

## `Notify rejected the credentials`

A `403`. The gateway answers a bad token and an unknown id identically, on
purpose, so it never confirms whether an id exists. Re-copy both values from
Notify → Settings → Device.

If you get `Those credentials belong to a Notify GROUP, not a device`, you
copied a group. A Live Activity is a tile on one specific device.

---

## Only some printers get tiles

Notify allows **five** live tiles per device. A sixth printer waits rather
than failing:

```json
"tiles_in_use": 5,
"tile_limit": 5,
"waiting_for_slot": ["bb3"]
```

When a tile ends, the freed slot goes to a print that is actually running
before one that is paused or silent, and within that group to whichever is
closest to finishing. To choose deliberately instead, use `PRINTER_ALLOWLIST`.

Also check `PRINTER_ALLOWLIST` is not already excluding the printer. It
matches the session key, never the printer name: `bb` plus BambuBuddy's
printer id, so `bb1` rather than `1`. `BAMBUDDY_PRINTER_IDS` takes the plain
ids and is easier to get right.

A printer that never shows up anywhere, tile or not, is more often the API
key's scope than the tile limit. See "Some printers never appear" above.

---

## A tile shows the wrong thing

### The countdown is wrong by a factor of 60

That is the specific bug this project was written to avoid, so please open an
issue. BambuBuddy reports `remaining_time` in minutes, and this service
multiplies by 60 in exactly one place.

### The tile is frozen at some percentage

Look at `seconds_since_telemetry` for that session. Above `SILENCE_AFTER`
(90s) the tile should read "Connection lost" rather than a frozen number. If
it does not, the service is not running.

A frozen tile is never an old reading arriving late. Every reading is answered
live and stamped as it arrives, so a reading's age is the round trip and
nothing else. A frozen tile means the polling stopped, and
`seconds_since_success` will say so.

### Progress starts high and jumps backwards

Should not happen: progress is pinned to 0 until it can be believed, because
BambuBuddy never resets its progress field at print start and the first
readings of a new print carry the last print's percentage. Worth an issue if
you see it.

The rule for believing a percentage is that it has to have climbed above the
one taken when the session started, since the stale carry-over this guards
against sits still rather than climbing.

### The tile has no layer counter, and a pause is not explained

Both are properties of the telemetry rather than faults. The status endpoint
reports no layer numbers, so there is no layer counter on the tile. It
reports no error codes, so a pause is shown as "Paused" and never as
"Filament runout". There is no plate-clear flag either.

The same shape explains a print that produced no tile at all. There are only
readings, so a terminal outcome is inferred from the printer going inactive
(`FINISH` is completed, `FAILED` is failed, anything else inactive is
aborted) after a 90 second grace, rather than being announced. A print that
both starts and finishes between two polls is never seen at all.
`BAMBUDDY_POLL_SECONDS` is the width of that window: 10 seconds by default.

---

## A tile came back after I swiped it away

It should not, until the next print starts. Dismissal is recorded locally,
keyed by printer, and cleared only on a genuine new-print signal.

The one case it cannot distinguish: an immediate reprint of the identical
file, dispatched while this service was not running. Nothing in the telemetry
separates that from the original print.

---

## After a restart, tiles disappeared or doubled

Check `STATE_DB` is on a persistent volume. Without one, every restart starts
blind: it cannot reattach to tiles still on the Lock Screen and cannot
remember dismissals.

With a volume, a restart reconciles every stored session against the gateway
before processing any telemetry, and adopts, forgets, or suppresses each one
accordingly.

---

## Reading the logs

Normal startup:

```
Notify credentials are valid: device ABC12345 (iPhone)
Recovering 2 session(s) from the previous run
Reattached to tile LA7Q2ZKM for printer bb1
Polling BambuBuddy at http://192.168.1.10:8000 every 10.0s
Discovery found 2 printer(s): 1 (Workshop H2C), 2 (Office X2D)
notify-bambuddy 0.1.0 is running
```

The URL is logged. The API key is not, ever.

Normal print:

```
Print started on Workshop H2C: Articulated Dragon
Started tile LA000042 for bb1
Ended tile LA000042 for bb1 (completed)
```

`LOG_LEVEL=DEBUG` adds every dropped duplicate, every suppressed update, and
every freshness rejection. It is noisy and it is the right thing to attach to
a bug report.

The device token and the BambuBuddy API key are both redacted from every log
line, including tracebacks raised from inside the HTTP library, and the whole
query string of any Notify URL is dropped before logging. You should never
find a credential in your logs; if you do, that is a security bug and worth
reporting as one.

---

## `Cannot create the state directory`

The container writes its database to `/data`, where the
`notify-bambuddy-state` volume is mounted. This means the volume is missing, or
`STATE_DB` was overridden to a path that is not writable inside the container.

Check the volume is actually declared and mounted, and if you set `STATE_DB`
yourself, that it points somewhere under `/data`.

Do not point it at a path that disappears when the container is recreated.
Losing the database costs tile reattachment and dismissal memory.

---

## Testing without a printer

```bash
docker compose run --rm -e DRY_RUN=1 notify-bambuddy
```

Tracks every printer and logs exactly what it would send, without creating,
updating or ending a single tile. Credential validation still happens. It
polls your real BambuBuddy exactly as usual and simply sends nothing to
Notify.

There is nowhere to inject a payload by hand, because the service asks the
questions itself rather than being told anything. What you can do instead is
ask the same question yourself and compare, with `LOG_LEVEL=DEBUG` running
alongside:

```bash
curl -s -H "X-API-Key: bb_..." http://HOST:8000/api/v1/webhook/printer/1/status
```

`remaining_time` in that reply is minutes. The debug log records the seconds
figure the tile was given, and it should be exactly sixty times the number in
the JSON.
