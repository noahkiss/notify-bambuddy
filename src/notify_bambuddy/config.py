# ---------------------------------------------------------------------------
# Configuration.
#
# Everything comes from environment variables, because that is what works
# identically in a Compose file, a `docker run`, and a plain shell. Every secret-bearing variable also accepts a `_FILE` suffixed
# companion pointing at a file, which is how Docker and Kubernetes secrets are
# mounted; the file form wins when both are set.
#
# The validation here is deliberately strict and deliberately loud. This
# service runs unattended for weeks, so a typo that silently produces "no
# tiles ever" is far more expensive than a startup crash with a clear message.
# Two checks in particular exist because the failure they prevent is otherwise
# silent:
#
#   * BAMBUDDY_URL is rejected if it carries a path, a query string, or points
#     at a page inside the UI rather than at the root. Pasting the address of
#     the API keys page is the obvious mistake, and it produces a 404 on every
#     poll with nothing to say why.
#
#   * The Notify credentials are checked against `/link` at startup, and a
#     GROUP credential is rejected explicitly. Live Activities require an
#     individual iOS device; a group id would otherwise fail later with the
#     API's uniform 403, which says only "Invalid token or id not found" and
#     gives the user nothing to act on.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import urllib.parse
import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# The Notify gateway.
#
# Hardcoded on purpose. There is no custom-gateway concept in the Notify API:
# self-hosting Notify means editing a constant in the iOS app source and
# rebuilding it. Exposing a configurable URL here would imply a portability
# that does not exist, and would add an SSRF-shaped hole for no user benefit.
#
# One thing NOT to be surprised by: the gateway's `/link` response echoes a
# `notification_url` built from its own older hostname
# (`notifypush.pingie.com`). Both names resolve to the same service and serve
# an identical API, verified side by side down to the error bodies. Nothing
# here reads that field, and nothing should: the base URL is this constant,
# not whatever the response happens to quote.
# ---------------------------------------------------------------------------
NOTIFY_BASE_URL = "https://push.getnotifyapp.com"


class ConfigError(Exception):
    """Raised for any configuration problem. The message is shown to the user."""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _read_secret(name: str, env: dict) -> Optional[str]:
    """Read a secret from `NAME_FILE` if present, else from `NAME`.

    The file form is how container secrets arrive: Docker mounts them under
    /run/secrets and Kubernetes under a volume, and neither shows up in
    `docker inspect` or in the process environment, unlike a plain variable.
    """
    path = env.get(name + "_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                # Strip trailing newline; `echo secret > file` adds one and it
                # is not part of the credential.
                return handle.read().strip() or None
        except OSError as exc:
            raise ConfigError(f"{name}_FILE is set to {path!r} but could not be read: {exc}")
    value = env.get(name)
    return value.strip() if value and value.strip() else None


def _str(name: str, env: dict, default: Optional[str] = None) -> Optional[str]:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _int(name: str, env: dict, default: int, minimum: Optional[int] = None,
         maximum: Optional[int] = None) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}, got {value}")
    return value


def _float(name: str, env: dict, default: float, minimum: Optional[float] = None,
           maximum: Optional[float] = None) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}, got {value}")
    return value


def _int_tuple(name: str, env: dict) -> tuple:
    """Parse a comma separated list of whole numbers into a tuple.

    Empty means "unset", which callers read as "no restriction". A list that
    is present but unparseable is an error rather than an empty tuple: a typo
    that silently widened the scope to every printer would be discovered on
    someone's Lock Screen.
    """
    raw = env.get(name)
    if raw is None or not raw.strip():
        return ()
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            raise ConfigError(f"{name} must be a comma separated list of printer ids, got {raw!r}")
    return tuple(values)


def _validate_bambuddy_url(url: str) -> str:
    """Normalize and sanity check the BambuBuddy base URL.

    Two mistakes are common enough to be worth catching at startup rather
    than as a confusing 404 on the first poll: pasting the address of a page
    inside the UI rather than the root, and omitting the scheme.
    """
    url = url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"BAMBUDDY_URL must start with http:// or https://, got {url!r}"
        )
    if not parsed.netloc:
        raise ConfigError(f"BAMBUDDY_URL is missing a host, got {url!r}")
    # `/settings?tab=apikeys` is the page people copy the key from, so it is
    # the path most likely to be pasted here by mistake.
    if parsed.path.rstrip("/") in ("/settings", "/printers", "/dashboard"):
        raise ConfigError(
            f"BAMBUDDY_URL should be BambuBuddy's base address, not a page inside it. "
            f"Use {parsed.scheme}://{parsed.netloc} rather than {url!r}."
        )
    if parsed.query or parsed.fragment:
        raise ConfigError(
            f"BAMBUDDY_URL should not carry a query string or fragment, got {url!r}"
        )
    return url


def _bool(name: str, env: dict, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _csv(name: str, env: dict) -> tuple[str, ...]:
    """Parse a comma-separated list, tolerating spaces and trailing commas."""
    raw = env.get(name)
    if not raw or not raw.strip():
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: Notify device ids come in two shapes, and which one you have decides what
#: this service can actually do:
#:
#:   8 characters          an iOS device. Can host a Live Activity.
#:   `WB` plus 14 more     a browser registered through the Notify web app.
#:                         Ordinary notifications work unchanged; a Live
#:                         Activity is an iOS Lock Screen feature and starting
#:                         one on a browser returns an honest 400.
#:
#: Both are accepted. A browser id puts the service into notifications-only
#: mode rather than being refused, because "your print finished" is most of
#: the value and there is no reason to withhold it from someone who reads
#: their notifications in a browser.
_IOS_DEVICE_ID_RE = re.compile(r"^[A-Z0-9]{8}$")
_WEB_DEVICE_ID_RE = re.compile(r"^WB[A-Za-z0-9]{14}$")

#: `#RRGGBB` or `#AARRGGBB`, with the leading `#` optional. The 8-digit form
#: puts ALPHA FIRST, which is the opposite of the CSS convention and worth
#: knowing before wondering why a colour came out transparent.
_TINT_RE = re.compile(r"^#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")

def _default_state_db(env: dict) -> str:
    """Where the SQLite state lives when nobody said.

    NOT `/data`. The container image sets `STATE_DB=/data/notify-bambuddy.db`
    explicitly and mounts a volume there, and that is the path that matters in
    practice. But running it straight from a checkout, which is what anyone
    working on the code does, is an ordinary account that cannot create
    `/data`, and the service would die on its first run with a permission
    error before it ever reached a log line.

    So the default follows the platform convention for per-user state, which
    is writable by definition: `$XDG_STATE_HOME` if set, else
    `~/.local/state/notify-bambuddy/state.db`.
    """
    base = env.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "notify-bambuddy", "state.db")
# ---------------------------------------------------------------------------
# The configuration object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Fully validated runtime configuration."""

    # --- BambuBuddy ---------------------------------------------------------
    bambuddy_url: str
    bambuddy_api_key: str
    bambuddy_poll_seconds: float
    bambuddy_discovery_seconds: float
    bambuddy_timeout: float
    #: Explicit printer ids to watch. Empty means "whatever the API key can
    #: see", which is the right default because the key's own printer scoping
    #: is already the place to express that.
    bambuddy_printer_ids: tuple

    # --- Notify destination ------------------------------------------------
    device_id: str
    device_token: str
    #: False for a browser subscription, which cannot host a Live Activity.
    #: The service then runs notifications-only: it still follows every print
    #: and still says when one finishes or fails, it just never puts a tile on
    #: a Lock Screen. Derived from the shape of the device id, not configured
    #: separately, because there is nothing for a user to decide here.
    tiles_enabled: bool

    # --- Presentation ------------------------------------------------------
    symbol: str
    tint: str

    #: Which kind of progress indicator the tile draws, if any. Notify has no
    #: "type" field: the fields present ARE the layout, and they compose. So
    #: this picks WHICH field carries progress rather than selecting a mode.
    #: "bar" sends `progress` (a smooth 0 to 100 bar), "segments" sends
    #: `steps`/`step` (discrete lit blocks), "off" sends neither and leaves a
    #: text-only tile that is still perfectly useful next to a countdown.
    tile_progress: str
    #: How many blocks a segmented bar has. Ignored unless tile_progress is
    #: "segments". Notify allows 2 to 20.
    tile_segments: int
    #: Adds up to six labelled chips (progress, time left) in place of the
    #: body line. Off by default because it REPLACES the model name, which is
    #: the thing most people want to see.
    tile_metrics: bool
    #: An optional tappable button. Empty disables it entirely.
    tile_button_url: Optional[str]
    tile_button_title: str
    #: True to have the phone OPEN the button's link, false to have it fire
    #: the request quietly in the background.
    tile_button_open: bool
    keep_for: int
    #: Hides print filenames from the tile, showing only the printer name,
    #: state, progress and ETA. For anyone whose Lock Screen is visible to
    #: other people and whose model names are not.
    privacy_mode: bool

    #: Which moments also get an ordinary push notification, on top of the
    #: tile updating. Live Activity traffic is deliberately kept out of
    #: Notify's history, so without this a finished print leaves no trace once
    #: its tile disappears.
    #:
    #:   "off"       tiles only
    #:   "problems"  failures, cancellations, filament runouts, silent printers
    #:   "all"       the above plus successful completions (default)
    alerts: str
    #: Sends problem alerts at the Time Sensitive interruption level, so they
    #: break through Do Not Disturb and Focus. Successful completions are
    #: never sent this way: nobody needs to be woken up by good news.
    alerts_time_sensitive: bool

    # --- Scope -------------------------------------------------------------
    #: When non-empty, only these printer serials get tiles. Everything else
    #: is ignored before any state is created for it.
    printer_allowlist: tuple[str, ...]
    #: How many tiles this service will hold at once. Notify's hard cap is 5
    #: per device; a lower value leaves room for other tools sharing the phone.
    max_tiles: int

    # --- Timing ------------------------------------------------------------
    #: A status frame older than this is not evidence of anything current.
    #: Retained payloads survive restarts and can be days old.
    retained_max_age_s: int
    #: No telemetry for this long means the numbers on the tile stopped being
    #: true, whatever `connected` claims.
    silence_after_s: int
    #: No telemetry for THIS long means the print is not coming back and the
    #: tile should go. Without it a printer that is switched off and left off
    #: keeps a "Connection lost" tile alive indefinitely, because the keepalive
    #: faithfully refreshes it and no terminal event will ever arrive.
    abandon_after_s: int
    #: A RUNNING printer with no session must stay RUNNING this long before a
    #: tile is created, so a single stale or flapping frame cannot spawn one.
    running_grace_s: int
    #: Floor between routine progress updates. Notify coalesces to one push
    #: per 10 seconds anyway; this keeps the HTTP traffic down too.
    progress_min_interval_s: int
    #: Unconditional refresh interval. Exists to keep the gateway's own
    #: housekeeping from reaping a healthy tile as abandoned, so it must
    #: bypass the redundant-payload check.
    keepalive_interval_s: int
    #: How long a terminal outcome is held before the tile is ended, so a
    #: contradicting event can still correct it.
    terminal_hold_s: int
    #: How long after a start to poll the gateway and confirm the tile
    #: actually appeared on the phone.
    start_verify_after_s: int
    #: Age at which a tile is rolled over to a successor. Apple ends every
    #: tile at 8 hours; rolling early avoids racing that ceiling.
    rollover_after_s: int
    #: Pause is not rendered for this long after a tile is created, because
    #: BambuBuddy's own plate detection can pause a print seconds after it
    #: starts and that momentary pause is not worth showing.
    pause_suppress_s: int

    # --- Local plumbing ----------------------------------------------------
    state_db: str
    health_enabled: bool
    health_host: str
    health_port: int
    log_level: str
    #: Logs what it would send without calling the Notify API. For pointing it
    #: at a real BambuBuddy and watching what it would do, safely.
    dry_run: bool

    #: Secret values, for the log redaction filter. Not configuration in its
    #: own right; a convenience so the filter can be installed from one place.
    secrets: tuple[str, ...] = field(default_factory=tuple)

    def allows(self, serial: str) -> bool:
        """True when this printer is in scope."""
        return not self.printer_allowlist or serial in self.printer_allowlist


def load_config(env: Optional[dict] = None) -> Config:
    """Build a Config from the environment, or raise ConfigError explaining why not."""
    env = dict(os.environ if env is None else env)

    # --- BambuBuddy --------------------------------------------------------
    bambuddy_url = _str("BAMBUDDY_URL", env, "")
    if not bambuddy_url:
        raise ConfigError(
            "BAMBUDDY_URL is required. It is the address of BambuBuddy itself, the same "
            "one you open in a browser, for example http://192.168.1.50:8000 or "
            "http://bambuddy:8000 for a Compose service name."
        )
    bambuddy_url = _validate_bambuddy_url(bambuddy_url)

    bambuddy_api_key = _read_secret("BAMBUDDY_API_KEY", env) or ""
    if not bambuddy_api_key:
        raise ConfigError(
            "BAMBUDDY_API_KEY is required. Create one in BambuBuddy under "
            "Settings -> API Keys. It needs the 'read_status' permission and nothing "
            "else: this service never controls a printer."
        )

    bambuddy_poll_seconds = _float("BAMBUDDY_POLL_SECONDS", env, 10.0, minimum=2.0, maximum=300.0)
    bambuddy_discovery_seconds = _float(
        "BAMBUDDY_DISCOVERY_SECONDS", env, 300.0, minimum=30.0, maximum=86400.0)
    bambuddy_timeout = _float("BAMBUDDY_TIMEOUT", env, 10.0, minimum=1.0, maximum=60.0)
    bambuddy_printer_ids = _int_tuple("BAMBUDDY_PRINTER_IDS", env)

    # --- Required: where the tiles go --------------------------------------
    device_id = _read_secret("NOTIFY_DEVICE_ID", env)
    device_token = _read_secret("NOTIFY_DEVICE_TOKEN", env)
    if not device_id or not device_token:
        raise ConfigError(
            "NOTIFY_DEVICE_ID and NOTIFY_DEVICE_TOKEN are required. Find both in the Notify "
            "app on the iPhone or iPad that should show the tiles, under Settings -> Device. "
            "Use a DEVICE, not a group: Live Activities are per-device and a group credential "
            "cannot host one."
        )
    # The GRP check comes FIRST and is separate, because a group id is exactly
    # 8 characters of A-Z0-9 and therefore passes the device-shape check. The
    # gateway itself distinguishes them by this prefix. Catching it here turns
    # what would otherwise be a uniform 403 saying "Invalid token or id not
    # found" into a message that tells the user what to do instead.
    if device_id.upper().startswith("GRP"):
        raise ConfigError(
            f"NOTIFY_DEVICE_ID {device_id!r} is a Notify GROUP id. A Live Activity is a tile "
            "on one specific iPhone or iPad, so it needs that device's own id and token from "
            "Settings -> Device in the Notify app."
        )
    if _IOS_DEVICE_ID_RE.match(device_id):
        tiles_enabled = True
    elif _WEB_DEVICE_ID_RE.match(device_id):
        # A browser. Everything except the tile still works.
        tiles_enabled = False
    else:
        raise ConfigError(
            f"NOTIFY_DEVICE_ID {device_id!r} is not a Notify device id. Expected either 8 "
            "characters (an iOS device, from Settings -> Device in the app) or 'WB' followed "
            "by 14 more (a browser registered through the Notify web app)."
        )

    # --- Presentation ------------------------------------------------------
    symbol = _str("NOTIFY_SYMBOL", env, "printer.fill")
    if len(symbol) > 64:
        raise ConfigError("NOTIFY_SYMBOL must be at most 64 characters")

    tint = _str("NOTIFY_TINT", env, "#00A76F")
    if not _TINT_RE.match(tint):
        raise ConfigError(
            f"NOTIFY_TINT {tint!r} is not a colour. Use #RRGGBB, or #AARRGGBB with the alpha "
            "channel FIRST. The leading '#' is optional."
        )
    # Normalize to the leading-# form. The API accepts both, and the bare form
    # exists only so the colour can travel in a URL query string, where a '#'
    # would start a fragment.
    tint = tint if tint.startswith("#") else "#" + tint

    # --- How the tile is laid out -----------------------------------------
    # Everything here is optional and defaults to what this service has always
    # sent, so an existing deployment sees no change. See README, "Choosing
    # how the tile looks".
    tile_progress = (_str("TILE_PROGRESS", env, "bar") or "bar").lower()
    if tile_progress not in ("bar", "segments", "off"):
        raise ConfigError(
            f"TILE_PROGRESS {tile_progress!r} is not a layout. Use 'bar' for a smooth "
            "percentage bar, 'segments' for discrete blocks, or 'off' for no bar at all."
        )
    tile_segments = _int("TILE_SEGMENTS", env, 10, minimum=2, maximum=20)

    tile_button_url = _str("TILE_BUTTON_URL", env, "") or ""
    if tile_button_url and not tile_button_url.startswith("https://"):
        raise ConfigError(
            f"TILE_BUTTON_URL {tile_button_url!r} must start with https://. Notify "
            "requires TLS for tile buttons, so a plain http:// address on your LAN "
            "cannot be used here. Leave it unset for no button."
        )
    if len(tile_button_url) > 512:
        raise ConfigError("TILE_BUTTON_URL is longer than the 512 characters Notify allows.")
    tile_button_title = _str("TILE_BUTTON_TITLE", env, "Open") or "Open"
    if len(tile_button_title) > 20:
        raise ConfigError(
            f"TILE_BUTTON_TITLE {tile_button_title!r} is longer than the 20 characters "
            "Notify allows on a button."
        )

    keep_for = _int("NOTIFY_KEEP_FOR", env, 300, minimum=0, maximum=14400)

    # --- Everything else ---------------------------------------------------
    alerts = (_str("ALERTS", env, "all") or "all").lower()
    if alerts not in ("off", "problems", "all"):
        raise ConfigError(
            f"ALERTS must be one of off, problems, all. Got {alerts!r}."
        )

    max_tiles = _int("MAX_TILES", env, 5, minimum=1, maximum=5)
    # An allowlist longer than MAX_TILES is legitimate (the extras queue for a
    # slot) and is warned about at startup in __main__, where a logger exists.
    allowlist = _csv("PRINTER_ALLOWLIST", env)

    silence_after = _int("SILENCE_AFTER", env, 90, minimum=30)
    abandon_after = _int("ABANDON_AFTER", env, 1800, minimum=120)
    if abandon_after <= silence_after:
        # Otherwise the tile is ended before it ever gets to say "Connection
        # lost", and the phase exists for nothing.
        raise ConfigError(
            f"ABANDON_AFTER ({abandon_after}) must be greater than SILENCE_AFTER "
            f"({silence_after}): a tile has to be able to show 'Connection lost' before "
            "it is given up on."
        )

    if not tiles_enabled and alerts == "off":
        raise ConfigError(
            f"NOTIFY_DEVICE_ID {device_id!r} is a browser subscription, which cannot host a "
            "Live Activity, and ALERTS is off. That combination leaves this service with "
            "nothing at all it is allowed to send. Set ALERTS to 'all' or 'problems', or "
            "use the device id of an iPhone or iPad from Settings -> Device in the app."
        )

    log_level = (_str("LOG_LEVEL", env, "INFO") or "INFO").upper()
    if log_level not in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        raise ConfigError(
            f"LOG_LEVEL {log_level!r} is not a level. Use one of CRITICAL, ERROR, "
            "WARNING, INFO, DEBUG."
        )

    config = Config(
        bambuddy_url=bambuddy_url,
        bambuddy_api_key=bambuddy_api_key,
        bambuddy_poll_seconds=bambuddy_poll_seconds,
        bambuddy_discovery_seconds=bambuddy_discovery_seconds,
        bambuddy_timeout=bambuddy_timeout,
        bambuddy_printer_ids=bambuddy_printer_ids,
        device_id=device_id,
        device_token=device_token,
        tiles_enabled=tiles_enabled,
        symbol=symbol,
        tint=tint,
        tile_progress=tile_progress,
        tile_segments=tile_segments,
        tile_metrics=_bool("TILE_METRICS", env, False),
        tile_button_url=tile_button_url or None,
        tile_button_title=tile_button_title,
        tile_button_open=_bool("TILE_BUTTON_OPEN", env, True),
        keep_for=keep_for,
        privacy_mode=_bool("PRIVACY_MODE", env, False),
        alerts=alerts,
        alerts_time_sensitive=_bool("ALERTS_TIME_SENSITIVE", env, True),
        printer_allowlist=allowlist,
        max_tiles=max_tiles,
        retained_max_age_s=_int("RETAINED_MAX_AGE", env, 180, minimum=30),
        silence_after_s=silence_after,
        abandon_after_s=abandon_after,
        running_grace_s=_int("RUNNING_GRACE", env, 20, minimum=0),
        progress_min_interval_s=_int("PROGRESS_MIN_INTERVAL", env, 15, minimum=10),
        keepalive_interval_s=_int("KEEPALIVE_INTERVAL", env, 1200, minimum=60, maximum=6000),
        terminal_hold_s=_int("TERMINAL_HOLD", env, 120, minimum=0, maximum=900),
        start_verify_after_s=_int("START_VERIFY_AFTER", env, 90, minimum=30, maximum=840),
        rollover_after_s=_int("ROLLOVER_AFTER", env, 27000, minimum=600, maximum=28200),
        pause_suppress_s=_int("PAUSE_SUPPRESS", env, 15, minimum=0, maximum=120),
        state_db=_str("STATE_DB", env, _default_state_db(env)),
        health_enabled=_bool("HEALTH_ENABLED", env, True),
        health_host=_str("HEALTH_HOST", env, "127.0.0.1"),
        health_port=_int("HEALTH_PORT", env, 9337, minimum=1, maximum=65535),
        log_level=log_level,
        dry_run=_bool("DRY_RUN", env, False),
        secrets=tuple(s for s in (device_token, bambuddy_api_key) if s),
    )
    return config
