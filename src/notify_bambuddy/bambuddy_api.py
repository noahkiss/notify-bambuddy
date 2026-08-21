# ---------------------------------------------------------------------------
# Talking to BambuBuddy directly, over its own REST API.
#
# BambuBuddy issues API keys (Settings -> API Keys) and exposes printer status
# under `/api/v1/webhook/`, so this service can just ask:
#
#     GET /api/v1/webhook/printer/{id}/status      X-API-Key: bb_...
#     -> {"id":1,"name":"Workshop H2C","connected":true,"state":"RUNNING",
#         "current_print":"Articulated Octopus","progress":50.0,
#         "remaining_time":44}
#
# One API key, one URL, no inbound port, and status at whatever cadence is
# asked for. BambuBuddy's own notification webhooks were the obvious
# alternative and are not used: their progress event fires only at the 25, 50
# and 75 percent milestones, and reports time remaining as a preformatted
# string like "2h 14m" rather than a number, which is no use to a countdown.
#
# WHAT THIS ROUTE DOES NOT CARRY, honestly:
#   - Layer numbers, so the tile has no layer counter.
#   - Error codes, so a pause is shown as "Paused" with no stated cause.
#   - Any printer serial. Printers are identified by BambuBuddy's own database
#     id, so sessions are keyed by that instead. See `synthetic_serial`.
#   - Discrete lifecycle events. A terminal outcome is inferred from the
#     printer's state going inactive, so a print that starts and finishes
#     entirely between two polls is never seen. `BAMBUDDY_POLL_SECONDS` bounds
#     that window.
#
# WHAT IT GETS IN RETURN: every reading is answered live and stamped on
# arrival, so a reading's age is the round trip. There is no cached copy of
# anything to go stale behind this service's back.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from . import USER_AGENT
from .models import PrinterStatus

log = logging.getLogger("notify_bambuddy.bambuddy")

#: The prefix BambuBuddy mounts its API-key-authenticated routes under.
API_ROOT = "/api/v1/webhook"

#: Printer ids are BambuBuddy database primary keys, which are stable for the
#: life of the printer's entry. They are turned into a session key with this
#: prefix so they can never collide with a real printer serial recorded by the
#: printer serial, so a stored session can never be mistaken for one keyed a
#: different way. It also reads as an identifier rather than as a stray number
#: in a log line.
SERIAL_PREFIX = "bb"

#: How long after a printer was last seen active the fast poll cadence is
#: kept. Sized to comfortably cover the coordinator's whole endgame: the 90
#: second inactive window that accepts the outcome, plus the 2 minute hold
#: before the tile is ended, plus slack. After this, a genuinely idle printer
#: drops back to the slow cadence.
ENDGAME_SECONDS = 300.0


def synthetic_serial(printer_id: int) -> str:
    """The stable per-printer key used everywhere a serial would be.

    Deliberately not the printer's name. Names are edited, and a rename would
    otherwise orphan the running tile and start a second one.
    """
    return f"{SERIAL_PREFIX}{printer_id}"


class BambuBuddyError(Exception):
    """Any failure talking to BambuBuddy."""


class BambuBuddyAuthError(BambuBuddyError):
    """The API key was rejected, or lacks a permission.

    Separated because it is the one failure that will never fix itself, and so
    the one worth refusing to start over.
    """


class BambuBuddyClient:
    """A very small HTTP client for BambuBuddy's API-key routes.

    urllib on a worker thread rather than a third-party HTTP library, matching
    `notify_client`. Two endpoints and no streaming does not justify a
    dependency, and keeping the dependency list at zero for this path is the
    entire point of it.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def get(self, path: str) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_blocking, path)

    def _get_blocking(self, path: str) -> Any:
        url = f"{self._base}{API_ROOT}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "X-API-Key": self._api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # The body often carries FastAPI's `{"detail": "..."}`, which names
            # the missing permission. Worth surfacing: "API key owner does not
            # have 'read_status' permission" is a fix, where "HTTP 403" is a
            # guess.
            detail = _detail(exc)
            if exc.code in (401, 403):
                raise BambuBuddyAuthError(
                    f"BambuBuddy rejected the API key (HTTP {exc.code}{detail}). Check "
                    f"BAMBUDDY_API_KEY against Settings -> API Keys, and that the key "
                    f"carries the 'read_status' permission for the printers you want."
                ) from None
            raise BambuBuddyError(f"HTTP {exc.code} from {path}{detail}") from None
        except urllib.error.URLError as exc:
            raise BambuBuddyError(f"Could not reach BambuBuddy at {self._base}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise BambuBuddyError(f"Could not reach BambuBuddy at {self._base}: {exc}") from None

        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            # Nearly always a reverse proxy or a login page answering instead
            # of BambuBuddy, so the message points at the URL rather than at
            # the JSON.
            raise BambuBuddyError(
                f"BambuBuddy returned something that is not JSON from {path} ({exc}). "
                f"Check that BAMBUDDY_URL points at BambuBuddy itself and not at a "
                f"login page or proxy in front of it."
            ) from None

    # --- The two endpoints this service uses -------------------------------

    async def printers(self) -> "list[tuple[int, str]]":
        """Discover printers as (id, name) pairs.

        There is no printer LIST route under the API-key scope, but `/queue`
        returns one entry per printer the key can see, which serves the same
        purpose and needs no extra permission: both routes require exactly
        `read_status`.
        """
        data = await self.get("/queue")
        if not isinstance(data, list):
            raise BambuBuddyError("The queue endpoint did not return a list")
        found = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            printer_id = entry.get("printer_id")
            if isinstance(printer_id, int):
                found.append((printer_id, str(entry.get("printer_name") or f"Printer {printer_id}")))
        return found

    async def status(self, printer_id: int) -> dict:
        data = await self.get(f"/printer/{printer_id}/status")
        if not isinstance(data, dict):
            raise BambuBuddyError(f"Status for printer {printer_id} was not an object")
        return data


def status_to_event(payload: dict, received_at: Optional[datetime] = None,
                    expected_id: Optional[int] = None) -> Optional[PrinterStatus]:
    """Turn one status response into the event the coordinator consumes.

    The timestamp is stamped HERE, on arrival, rather than read from the
    payload. That is not a shortcut: this response was generated to answer
    this request, so its age is the round trip and nothing else. It is the one
    request, so its age is the round trip and nothing else.

    `expected_id` is the printer this response was ASKED about. When it is
    given, a body claiming to be a different printer is discarded rather than
    believed. Identity then comes from the request rather than from the
    response. Without it a single misrouted or malformed response could open
    a session against, or write progress into, the wrong printer's tile.
    """
    printer_id = payload.get("id")
    if not isinstance(printer_id, int):
        return None
    if expected_id is not None and printer_id != expected_id:
        log.warning(
            "Discarding a status response for printer %s that arrived while asking "
            "about printer %s", printer_id, expected_id,
        )
        return None

    # `remaining_time` is MINUTES, not seconds. BambuBuddy stores the
    # firmware's `mc_remaining_time` verbatim and this route passes it through
    # untouched, while other call sites inside BambuBuddy multiply by 60
    # before handing it to code that wants seconds. Reading it as seconds is
    # the single highest-consequence mistake available in this integration: it
    # turns a two hour print into a two minute countdown.
    remaining = payload.get("remaining_time")
    remaining_minutes = int(remaining) if isinstance(remaining, (int, float)) else 0

    progress = payload.get("progress")
    progress_value = float(progress) if isinstance(progress, (int, float)) else 0.0

    current_print = payload.get("current_print") or None

    return PrinterStatus(
        serial=synthetic_serial(printer_id),
        printer_id=printer_id,
        printer_name=str(payload.get("name")) if payload.get("name") else None,
        timestamp=received_at or datetime.now(timezone.utc),
        connected=bool(payload.get("connected")),
        state=str(payload.get("state") or "").upper() or "unknown",
        progress=progress_value,
        remaining_minutes=remaining_minutes,
        # Absent from this API. Zero is the "unknown" value the content
        # builder already understands, so the tile simply omits the layer line
        # rather than showing a wrong one.
        layer_num=0,
        total_layers=0,
        # `current_print` is BambuBuddy's own display name, already resolved
        # from subtask_name or the gcode path. Both slots get it because the
        # content builder prefers subtask_name and falls back to gcode_file,
        # and here there is only the one value.
        subtask_name=current_print,
        gcode_file=current_print,
        current_print=current_print,
        # Not reported by this route. False means "no information", which is
        # how every consumer already reads it.
        awaiting_plate_clear=False,
    )


class APISource:
    """Polls BambuBuddy and pushes status events onto the queue.

    There is no background thread: the poll loop is an asyncio task on the
    same loop as everything else, so nothing here is shared across threads and
    nothing needs a lock.
    """

    def __init__(self, config, loop: asyncio.AbstractEventLoop,
                 queue: "asyncio.Queue[Any]") -> None:
        self._config = config
        self._loop = loop
        self._queue = queue
        self._client = BambuBuddyClient(
            config.bambuddy_url, config.bambuddy_api_key, timeout=config.bambuddy_timeout
        )

        self._task: Optional[asyncio.Task] = None
        self._printers: "dict[int, str]" = {}
        self._printers_checked_at: float = 0.0

        self._polls = 0
        self._events = 0
        self._consecutive_failures = 0
        self._last_success_at: Optional[float] = None
        self._last_error: Optional[str] = None

        #: When each printer was last seen in an active state, which is what
        #: decides the poll interval. An instance attribute, not a class one:
        #: a container defined in the class body is shared by every instance
        #: ever created, which is invisible in production with a single source
        #: and silently cross-contaminates every test in a suite.
        self._active_since: dict = {}

    # --- Lifecycle ---------------------------------------------------------

    async def validate(self) -> None:
        """Check the credentials before the service commits to running.

        The analogue of the Notify credential check at startup, and worth the
        same one round trip: a rejected API key produces silence otherwise,
        and silence is indistinguishable from "no printers are running".
        """
        printers = await self._client.printers()
        self._printers = dict(printers)
        self._printers_checked_at = time.time()
        if not printers:
            log.warning(
                "BambuBuddy accepted the API key but reports no printers. If the key is "
                "restricted to specific printers, check that list."
            )
        else:
            log.info(
                "BambuBuddy has %d printer(s): %s",
                len(printers),
                ", ".join(f"{name} (id {pid})" for pid, name in sorted(printers)),
            )

    def start(self) -> None:
        self._task = self._loop.create_task(self._run(), name="bambuddy-poll")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # --- The poll loop -----------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive any single failure
                log.exception("Unexpected failure in the BambuBuddy poll loop")
            await asyncio.sleep(self._interval())

    def _interval(self) -> float:
        """How long to wait before the next poll.

        Three regimes, in priority order:

        1. Backing off after failures. Doubling from the configured interval
           to a minute, so a BambuBuddy that is restarting is not hammered and
           a wrong URL does not fill the log at one line per poll.
        2. Something is printing: the configured interval.
        3. Everything is idle: eight times slower, floor of 30 seconds. An
           idle farm needs no attention, and a print starting is noticed
           within that window, which is well inside the minutes a printer
           spends heating and calibrating before there is anything to show.
        """
        base = self._config.bambuddy_poll_seconds
        normal = base if self._anything_active else max(30.0, base * 8)
        if not self._consecutive_failures:
            return normal
        # Backoff is a multiplier on the NORMAL interval, not an independent
        # schedule. Computed from `base` alone it could come out shorter than
        # the idle cadence, which would mean a BambuBuddy that is down gets
        # polled harder than one that is merely quiet.
        return min(120.0, normal * (2 ** min(self._consecutive_failures, 4)))

    @property
    def _anything_active(self) -> bool:
        cutoff = time.time() - ENDGAME_SECONDS
        return any(seen > cutoff for seen in self._active_since.values())

    async def _poll_once(self) -> None:
        # Rediscover periodically so a printer added in BambuBuddy is picked
        # up without a restart. Cheap, and only every few minutes.
        if time.time() - self._printers_checked_at > self._config.bambuddy_discovery_seconds:
            try:
                self._printers = dict(await self._client.printers())
                self._printers_checked_at = time.time()
            except BambuBuddyError as exc:
                # Not fatal: the previously discovered set is still usable, and
                # the per-printer polls below will report the real problem.
                log.debug("Could not refresh the printer list: %s", exc)

        targets = self._targets()
        if not targets:
            return

        for printer_id in targets:
            try:
                payload = await self._client.status(printer_id)
            except BambuBuddyAuthError as exc:
                # Logged once per transition rather than once per poll. A key
                # whose permissions were revoked mid-run should say so clearly
                # and then stop shouting.
                self._fail(str(exc))
                return
            except BambuBuddyError as exc:
                self._fail(str(exc))
                return

            self._succeed()
            event = status_to_event(payload, expected_id=printer_id)
            if event is None:
                log.debug("Ignoring an unrecognisable status payload for printer %s", printer_id)
                continue
            self._track(event)
            self._enqueue(event)

    def _targets(self) -> "list[int]":
        """Which printer ids to poll.

        An explicit allowlist wins over discovery, because someone who has
        named their printers has said which ones they care about and should
        not have a newly added one appear on their Lock Screen.
        """
        configured = self._config.bambuddy_printer_ids
        if configured:
            return list(configured)
        return sorted(self._printers)

    # --- Bookkeeping -------------------------------------------------------

    def _enqueue(self, event: PrinterStatus) -> None:
        try:
            self._queue.put_nowait(event)
            self._events += 1
        except asyncio.QueueFull:
            # The queue is bounded because an unbounded one turns a wedged
            # coordinator into an out-of-memory kill. Dropping a poll is free,
            # since the next one carries the same state.
            log.warning("Event queue is full; dropped a status poll for %s", event.serial)

    def _track(self, event: PrinterStatus) -> None:
        """Note when a printer was last seen doing something.

        The fast cadence is kept for ENDGAME_SECONDS after a printer LEAVES an
        active state, not dropped at that instant. The moment a print stops is
        exactly when the coordinator starts timing things: a 90 second window
        before the outcome is accepted, then a 2 minute hold before the tile
        ends. Falling back to the idle cadence right then stretched a 90
        second window to 160 seconds in practice, because the readings that
        would close it only arrived every 80.
        """
        if event.state in ("RUNNING", "PAUSE", "PREPARE"):
            self._active_since[event.serial] = time.time()

    def _succeed(self) -> None:
        if self._consecutive_failures:
            log.info("BambuBuddy is reachable again")
        self._consecutive_failures = 0
        self._last_error = None
        self._last_success_at = time.time()
        self._polls += 1

    def _fail(self, message: str) -> None:
        first = self._consecutive_failures == 0
        self._consecutive_failures += 1
        self._last_error = message
        if first:
            log.error("%s", message)
        else:
            log.debug("Still failing (%d in a row): %s", self._consecutive_failures, message)

    # --- Diagnostics -------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "source": "api",
            "url": self._config.bambuddy_url,
            "printers_known": {str(pid): name for pid, name in sorted(self._printers.items())},
            "printers_polled": self._targets(),
            "in_endgame": self._anything_active,
            "polls_succeeded": self._polls,
            "events_emitted": self._events,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "seconds_since_success": (
                round(time.time() - self._last_success_at, 1)
                if self._last_success_at is not None else None
            ),
            "poll_interval": round(self._interval(), 1),
        }


def _detail(exc: urllib.error.HTTPError) -> str:
    """Pull FastAPI's `{"detail": ...}` out of an error body, if it is there."""
    try:
        body = exc.read()
    except Exception:  # noqa: BLE001
        return ""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return f": {detail}" if detail else ""
