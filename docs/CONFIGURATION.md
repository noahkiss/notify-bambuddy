# Configuration reference

Everything is an environment variable. Four are required; the rest have
defaults chosen for a home setup with one to five printers.

Any variable holding a secret also accepts a `_FILE` suffixed companion
pointing at a file, which is how Docker and Kubernetes secrets are mounted.
The file form wins when both are set, and it keeps the value out of the
process environment and out of `docker inspect`:

```bash
NOTIFY_DEVICE_TOKEN_FILE=/run/secrets/notify_token
```

---

## Required

| Variable | Description |
|---|---|
| `BAMBUDDY_URL` | BambuBuddy's base address, exactly as you browse to it, with no path or query. For example `http://192.168.1.50:8000`, or `http://bambuddy:8000` for a Compose service name. |
| `BAMBUDDY_API_KEY` | An API key from BambuBuddy, **Settings → API Keys**, carrying the `read_status` permission. **A credential.** Also accepts `BAMBUDDY_API_KEY_FILE`. |
| `NOTIFY_DEVICE_ID` | From Notify → Settings → Device. Eight characters for an iPhone or iPad, or `WB` plus 14 more for a browser subscription. A browser cannot host a Live Activity, so that id puts the service into notifications-only mode: no tile, but every print is still followed and still announced. It is worked out from the id, with nothing to set. |
| `NOTIFY_DEVICE_TOKEN` | From the same screen. **A credential.** |

The API key travels in an `X-API-Key` header, never in a URL, and is redacted
from logs alongside the device token.

`NOTIFY_DEVICE_ID` must be a **device**, not a group. A group id starts with
`GRP` and is rejected at startup with an explanation, because the Live
Activity endpoints would otherwise answer it with a uniform `403` saying only
"Invalid token or id not found". A `WB`-prefixed 16-character id is a web-push
browser registration and cannot host a Live Activity either.

## Polling

Two endpoints are read, both needing only the `read_status` permission: the
webhook queue, to discover which printers exist, and each printer's status,
for the telemetry itself.

| Variable | Default | Notes |
|---|---|---|
| `BAMBUDDY_POLL_SECONDS` | `10` | Seconds between polls while something is printing. 2 to 300. Everything idle slows to eight times this with a 30 second floor. |
| `BAMBUDDY_PRINTER_IDS` | all | Comma-separated BambuBuddy printer ids, for example `1,3`. Checked before a request is made, so it is cheaper and clearer than `PRINTER_ALLOWLIST`. |
| `BAMBUDDY_DISCOVERY_SECONDS` | `300` | How often to re-check which printers exist, so one added in BambuBuddy appears without a restart. |
| `BAMBUDDY_TIMEOUT` | `10` | HTTP timeout for one request, in seconds. |

Polling faster does not cost extra pushes to the phone. The tile's countdown
is relative and iOS ticks it down on the device, so a whole print costs a
handful of pushes at any poll rate. What the interval bounds is how long a
finished print can go unnoticed, and how quickly a pause shows up.

A failing BambuBuddy is backed off by doubling the normal interval up to a
120 second cap, and never polled faster than the healthy cadence.

What the API reports bounds what a tile can say. There is no layer number in
the payload, so the tile carries no layer counter, and there are no error
codes, so a pause reads "Paused" with no cause. There is no plate-clear flag.
Terminal outcomes are inferred from a printer going inactive (`FINISH` is
completed, `FAILED` is failed, anything else inactive is aborted) after a 90
second grace, rather than arriving as an event, and a print that both starts
and finishes between two polls is never seen at all. `BAMBUDDY_POLL_SECONDS`
bounds that window.

---

## Appearance

| Variable | Default | Notes |
|---|---|---|
| `NOTIFY_SYMBOL` | `printer.fill` | An SF Symbol name. |
| `NOTIFY_TINT` | `#00A76F` | `#RRGGBB` or `#AARRGGBB`. |
| `NOTIFY_KEEP_FOR` | `300` | Seconds a finished tile lingers, 0 to 14400. |
| `PRIVACY_MODE` | `false` | Keep model and file names off the Lock Screen. |

**Nothing validates the SF Symbol name**, anywhere in the stack. The gateway
checks only that it is a string under 64 characters, and the widget passes it
straight to the system, where an unknown name renders as a blank space with no
error. Check any replacement against the SF Symbols catalogue.

**The 8-digit tint form puts alpha first** (`#AARRGGBB`), which is the
opposite of the CSS convention. The leading `#` is optional because the value
can also travel in a URL, where a `#` would start a fragment.

---

## Tile layout

Every option here is optional, and the defaults are what this service has
always sent. Notify has no "tile type" field: the fields present in a payload
ARE the layout, and they compose, so these are independent switches.

| Variable | Default | Range | What it does |
|---|---|---|---|
| `TILE_PROGRESS` | `bar` | `bar`, `segments`, `off` | Which progress indicator to draw |
| `TILE_SEGMENTS` | `10` | 2 to 20 | Blocks in a segmented bar |
| `TILE_METRICS` | `false` | bool | Chips in place of the model name |
| `TILE_BUTTON_URL` | unset | https only, 512 chars | Adds a tappable button |
| `TILE_BUTTON_TITLE` | `Open` | 20 chars | The button's label |
| `TILE_BUTTON_OPEN` | `true` | bool | Open the link, or fire it silently |

**`TILE_PROGRESS`.** `bar` sends Notify's `progress` field (0 to 100) and draws
a continuous bar. `segments` sends `steps` and `step` instead, drawing discrete
blocks. `off` sends neither. Exactly one of these is ever sent, and the unused
one is explicitly cleared rather than omitted, so changing this setting cannot
leave a tile drawing two bars at once.

A segment lights only when its share of the print is fully finished, which
rounds down. Ten segments means the tenth lights at 100%, not at 95%. Rounding
to nearest would make a long print appear finished for its final minutes.

**`TILE_METRICS`.** Sends up to three chips: progress as a percentage, time
left in minutes, and the status word. These take the place of the body line, so
turning them on removes the model name from the tile. That trade is why it is
off by default. A chip whose value ends in `%` renders its own miniature bar,
which is why progress is sent as `61%` rather than `61` with a unit of `%`.

**`TILE_BUTTON_URL`.** Rejected at startup unless it begins with `https://`.
That is Notify's requirement, not this service's, and catching it at startup is
better than a button that silently does nothing when tapped. With
`TILE_BUTTON_OPEN=false` the phone performs the request itself without opening
anything, which suits a webhook more than a link.

Layout changes take effect on the next tile. A print already running keeps the
layout it started with, so nothing rearranges itself mid-print.

## Alerts

| Variable | Default | Notes |
|---|---|---|
| `ALERTS` | `all` | `all`, `problems`, or `off`. |
| `ALERTS_TIME_SENSITIVE` | `true` | Problem alerts break through Do Not Disturb and Focus. |

Time Sensitive is a request, not a guarantee. iOS delivers at the normal level
instead, silently, if the user has turned it off for Notify in their Focus
settings. Successful completions are never sent this way regardless of the
setting.

---

## Scope

| Variable | Default | Notes |
|---|---|---|
| `PRINTER_ALLOWLIST` | all printers | Comma-separated session keys, for example `bb1,bb3`. Prefer `BAMBUDDY_PRINTER_IDS`, which takes plain ids and is checked before a request is made. |
| `MAX_TILES` | `5` | Notify's limit is 5 live tiles per device. |

A session key is `bb` followed by BambuBuddy's database id for the printer,
because the API exposes no serial to key on. Those ids are stable for the life
of the printer's entry, including across renames, which is more than can be
said for the names themselves.

Setting `MAX_TILES` lower than 5 leaves room for other tools sharing the same
phone. Printers beyond the limit are not dropped: they queue, and a freed slot
goes to a print that is actually running before one that is paused or silent,
and within that group to whichever is closest to finishing. A paused print's
ETA is frozen at whatever it said when it stopped moving, so ranking on that
alone would let it jump the queue.

---

## Local

| Variable | Default | Notes |
|---|---|---|
| `STATE_DB` | `~/.local/state/notify-bambuddy/state.db`, or `/data/notify-bambuddy.db` in the container | **Put this on a persistent volume.** The container sets it to `/data`, where the `notify-bambuddy-state` volume is mounted, and that is the value that matters. Run from a checkout instead and it defaults to the platform per-user state directory, following `XDG_STATE_HOME` when that is set. |
| `HEALTH_ENABLED` | `true` | |
| `HEALTH_HOST` | `127.0.0.1` (`0.0.0.0` in the container image) | |
| `HEALTH_PORT` | `9337` | |
| `LOG_LEVEL` | `INFO` | |
| `DRY_RUN` | `false` | Track everything; create, update and end nothing. Credential validation and status reads still happen. |

Losing the state database means a restart cannot reattach to tiles that are
still on the Lock Screen, cannot remember which ones were swiped away, and
cannot tell whether a start that was in flight when the process stopped
actually created a tile. All three are avoidable with a volume.

---

## Timing

Every default below has a reason. Changing them is supported and rarely
necessary.

| Variable | Default | What breaks if it is wrong |
|---|---|---|
| `RETAINED_MAX_AGE` | `180` | The oldest a reading may be and still be acted on. Every reading is answered live and stamped on arrival, so its age is the round trip to BambuBuddy and the gate is satisfied by construction. It stays because it is cheap, and because a reading that somehow arrives older than this should become nothing rather than a tile. |
| `SILENCE_AFTER` | `90` | How long without telemetry before the tile says "Connection lost". Readings arrive every `BAMBUDDY_POLL_SECONDS` while printing, and only every 80 seconds at the default interval once everything is idle, so lowering this produces spurious "Connection lost". |
| `ABANDON_AFTER` | `1800` (must exceed `SILENCE_AFTER`) | How long without telemetry before the tile is ended outright. A printer switched off at the wall goes quiet without its state ever going inactive, so no terminal outcome is coming; without this the keepalive would hold a "Connection lost" tile until Apple's 8 hour ceiling and the rollover would then start another. |
| `RUNNING_GRACE` | `20` | How long a printer must keep reporting a running print before a tile is created for it. Guards against a single flapping reading. |
| `PROGRESS_MIN_INTERVAL` | `15` | Floor between routine progress pushes. Notify coalesces to one push per 10 seconds anyway; phase changes bypass this. |
| `KEEPALIVE_INTERVAL` | `1200` | **Must stay under 2 hours.** Notify ends any tile without an end time that has gone 2 hours without an update, and a paused or ETA-unknown print is exactly that shape. |
| `TERMINAL_HOLD` | `120` | How long a finished outcome is held before the tile ends, so a contradicting event can correct it. Set to 0 to end immediately, at the cost of occasionally showing "Cancelled" for a print that succeeded. |
| `START_VERIFY_AFTER` | `90` | When to check whether the gateway has heard that a started tile reached the phone. Informational only: an unconfirmed tile is logged and nothing else, because the app confirms tiles only while it is open and a healthy tile is routinely unconfirmed. |
| `ROLLOVER_AFTER` | `27000` | 7.5 hours. Apple ends every tile at 8 hours (28800) regardless; rolling over early avoids racing that. |
| `PAUSE_SUPPRESS` | `15` | Ignore a pause this soon after a tile is created. BambuBuddy's plate detection can pause a print seconds after it starts. |

The reasoning behind each of these, and the upstream behaviour that motivates
it, is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Health endpoint

```bash
curl -s localhost:9337/status | python3 -m json.tool
```

| Path | Meaning |
|---|---|
| `/healthz` | The process is up. Deliberately does not depend on BambuBuddy being reachable: restarting will not fix a BambuBuddy outage, and a restart loop during one only makes recovery slower. |
| `/readyz` | The last poll succeeded and at least one ever has. `503` while starting up, and while BambuBuddy is refusing or unreachable. |
| `/status` | Sessions, tile budget, and the poll counters. |

Those counters live under `source`: `url`, `printers_known`,
`printers_polled`, `polls_succeeded`, `events_emitted`,
`consecutive_failures`, `last_error`, `seconds_since_success` and
`poll_interval`. The pair worth knowing is `last_error` with
`seconds_since_success`: together they separate "nothing is printing" from
"BambuBuddy is not answering".

No credential ever appears in this output.
