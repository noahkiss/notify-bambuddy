# ---------------------------------------------------------------------------
# Shared test fixtures.
#
# Three things live here: a fake Notify gateway, a fake clock, and a builder
# for the one event the service consumes.
#
# That event is `PrinterStatus`, and `status()` below is how tests produce it.
# Its units are taken from BambuBuddy's actual source rather than from its
# wiki, because the two disagree in ways that would make a suite built on the
# wiki pass while the real integration failed: the wiki's own example shows
# `remaining_time: 3600` alongside 45.5% progress, which reads exactly like
# seconds and is in fact minutes. `remaining_minutes` carries that name so the
# mistake cannot be made silently here.
#
# No test here touches a real network, a real printer, or a real Notify
# account.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from notify_bambuddy import coordinator as coordinator_module
from notify_bambuddy.alerts import Alerts
from notify_bambuddy.config import Config
from notify_bambuddy.coordinator import Coordinator
from notify_bambuddy.models import PrinterStatus
from notify_bambuddy.notify_client import ActivityStatus, NotifyError, StartedActivity
from notify_bambuddy.store import Store

SERIAL = "00M09C411500579"
SERIAL_B = "01P00A999888777"


def make_config(**overrides: Any) -> Config:
    """A Config with test-friendly timings.

    Grace periods are zeroed unless a test is specifically about them, so the
    suite does not spend real seconds waiting for a state machine that is
    working correctly.
    """
    values = dict(
        # The BambuBuddy connection details are blank by default. Most of the
        # suite drives the coordinator with `status()` events directly and
        # never opens a connection at all; the tests that do exercise the REST
        # transport override `bambuddy_url` and `bambuddy_api_key` explicitly.
        bambuddy_url="",
        bambuddy_api_key="",
        bambuddy_poll_seconds=10.0,
        bambuddy_discovery_seconds=300.0,
        bambuddy_timeout=10.0,
        bambuddy_printer_ids=(),
        device_id="ABC12345",
        device_token="XYZ789TOKEN123",
        symbol="printer.fill",
        tint="#00A76F",
        keep_for=300,
        privacy_mode=False,
        alerts="all",
        alerts_time_sensitive=True,
        printer_allowlist=(),
        max_tiles=5,
        retained_max_age_s=180,
        silence_after_s=90,
        abandon_after_s=1800,
        running_grace_s=0,
        progress_min_interval_s=15,
        keepalive_interval_s=1200,
        terminal_hold_s=120,
        start_verify_after_s=90,
        rollover_after_s=27000,
        pause_suppress_s=0,
        state_db=":memory:",
        health_enabled=False,
        health_host="127.0.0.1",
        health_port=9337,
        log_level="INFO",
        dry_run=False,
        secrets=("XYZ789TOKEN123",),
    )
    values.update(overrides)
    return Config(**values)


class FakeNotify:
    """A stand-in for the Notify gateway that records everything.

    Faithful to the behaviours the coordinator actually depends on:

      * `start` enforces the five-tile cap and raises the same 400 the real
        gateway does, message and all, because the coordinator recognises that
        one transient 400 by its wording.
      * `end` is idempotent.
      * Errors are queued by `fail_next`, so a test can make one specific call
        fail without touching any other.
    """

    def __init__(self, max_tiles: int = 5) -> None:
        self.max_tiles = max_tiles
        self.started: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.ended: list[tuple[str, dict, int]] = []
        self.notifications: list[dict] = []
        self.states: dict[str, ActivityStatus] = {}
        self._counter = 0
        self._fail_next: dict[str, list[Exception]] = {}
        self.link_response: dict = {"success": True, "type": "device", "id": "ABC12345",
                                    "name": "iPhone"}

    # --- Test controls -----------------------------------------------------

    def fail_next(self, method: str, error: Exception) -> None:
        """Make the next call to `method` raise `error`."""
        self._fail_next.setdefault(method, []).append(error)

    def _maybe_fail(self, method: str) -> None:
        queued = self._fail_next.get(method)
        if queued:
            raise queued.pop(0)

    def live_ids(self) -> set:
        return {aid for aid, state in self.states.items() if state.is_live}

    # --- API ---------------------------------------------------------------

    async def link(self) -> dict:
        self._maybe_fail("link")
        return self.link_response

    async def start(self, content: dict) -> StartedActivity:
        self._maybe_fail("start")
        if len(self.live_ids()) >= self.max_tiles:
            from notify_bambuddy.notify_client import NotifyValidationError
            raise NotifyValidationError(
                "This device already has the maximum of 5 live activities", 400
            )
        self._counter += 1
        activity_id = f"LA{self._counter:06d}"
        self.started.append({"activityId": activity_id, "content": dict(content)})
        # `expiresAt` is stamped from the COORDINATOR's clock, which the
        # `clock` fixture replaces with a fake one. Stamping it from the real
        # clock would put Apple's eight-hour ceiling in the past relative to
        # the fake "now", and the rollover would fire on the very next tick.
        started_at = datetime.fromtimestamp(coordinator_module.time.time(), tz=timezone.utc)
        self.states[activity_id] = ActivityStatus(
            activity_id=activity_id, state="active", end_reason=None,
            expires_at=(started_at + timedelta(hours=8)).isoformat(),
            raw={"content": dict(content)},
        )
        return StartedActivity(activity_id=activity_id,
                               expires_at=self.states[activity_id].expires_at)

    async def update(self, activity_id: str, content: dict) -> None:
        self._maybe_fail("update")
        state = self.states.get(activity_id)
        if state is None or not state.is_live:
            from notify_bambuddy.notify_client import NotifyGone
            # Deliberately WITHOUT an end_reason. The real gateway's 410 body
            # carries prose, not a machine-readable reason: the reason only
            # comes from a follow-up status read, which is exactly the read
            # that can fail. A fake that hands the reason over for free would
            # hide every bug in that path.
            raise NotifyGone("This Live Activity has ended", 410)
        self.updates.append((activity_id, dict(content)))

    async def end(self, activity_id: str, content: Optional[dict] = None,
                  keep_for: int = 0) -> None:
        self._maybe_fail("end")
        self.ended.append((activity_id, dict(content or {}), keep_for))
        state = self.states.get(activity_id)
        if state is not None:
            self.states[activity_id] = ActivityStatus(
                activity_id=activity_id, state="ended", end_reason="script",
                expires_at=state.expires_at, raw={},
            )

    async def status(self, activity_id: str) -> ActivityStatus:
        self._maybe_fail("status")
        state = self.states.get(activity_id)
        if state is None:
            raise NotifyError("Invalid token or id not found", 403)
        return state

    async def list_device(self) -> tuple:
        self._maybe_fail("list_device")
        return tuple(state for state in self.states.values() if state.is_live)

    async def classify_gone(self, activity_id: str, error):
        """Mirrors the real client: the reason comes from a status READ, which
        can itself fail. Routing through `self.status` is what lets a test
        inject that failure with `fail_next("status", ...)`."""
        from notify_bambuddy.notify_client import NotifyError, NotifyGone
        if error.end_reason:
            return error
        try:
            state = await self.status(activity_id)
        except NotifyError:
            return error
        reason = state.end_reason
        if state.state == "dismissed":
            reason = "dismissed"
        return NotifyGone(str(error), error.status, error.body, end_reason=reason)

    async def notify(self, text: str, title: str, group_type: str,
                     time_sensitive: bool = False) -> None:
        self._maybe_fail("notify")
        self.notifications.append({
            "text": text, "title": title, "groupType": group_type,
            "timeSensitive": time_sensitive,
        })

    # --- Simulating what the user or the gateway does ----------------------

    def dismiss(self, activity_id: str) -> None:
        """The user swipes the tile away."""
        self.states[activity_id] = ActivityStatus(
            activity_id=activity_id, state="dismissed", end_reason="dismissed",
            expires_at=None, raw={},
        )

    def reap(self, activity_id: str, reason: str = "overdue") -> None:
        """The gateway's housekeeping ends a tile whose print is still running."""
        self.states[activity_id] = ActivityStatus(
            activity_id=activity_id, state="ended", end_reason=reason,
            expires_at=None, raw={},
        )

    def never_started(self, activity_id: str) -> None:
        """The push was accepted but no tile ever appeared on the phone."""
        self.states[activity_id] = ActivityStatus(
            activity_id=activity_id, state="starting", end_reason=None,
            expires_at=None, raw={},
        )


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def fake_notify() -> FakeNotify:
    return FakeNotify()


# ---------------------------------------------------------------------------
# The coordinator test harness
#
# Shared by the scenario tests and the regression tests, so neither has to
# import from the other.
# ---------------------------------------------------------------------------


class Clock:
    """A stand-in for the `time` module, exposing only what is used."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def moment(self, offset: float = 0.0) -> datetime:
        """A source timestamp matching the fake clock, so freshness checks pass."""
        return datetime.fromtimestamp(self.t + offset, tz=timezone.utc)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(coordinator_module, "time", fake)
    return fake


def run(coro):
    """Drive one coroutine to completion."""
    return asyncio.new_event_loop().run_until_complete(coro)


def build(config=None, notify=None, with_alerts: bool = False):
    """A coordinator wired to an in-memory store and a fake gateway."""
    config = config or make_config()
    notify = notify or FakeNotify(max_tiles=config.max_tiles)
    store = Store(":memory:")
    alerts = Alerts(config, notify) if with_alerts else None
    return Coordinator(config, store, notify, alerts=alerts), notify, store


def status(clock: Clock, *, serial: str = SERIAL, state: str = "RUNNING",
           progress: float = 10.0, remaining_minutes: int = 100,
           layer_num: int = 10, total_layers: int = 300, connected: bool = True,
           subtask_name: str = "Benchy", offset: float = 0.0,
           awaiting_plate_clear: bool = False, printer_name: str = "X1C-1") -> PrinterStatus:
    return PrinterStatus(
        serial=serial,
        printer_id=1,
        printer_name=printer_name,
        timestamp=clock.moment(offset),
        connected=connected,
        state=state,
        progress=progress,
        remaining_minutes=remaining_minutes,
        layer_num=layer_num,
        total_layers=total_layers,
        subtask_name=subtask_name,
        gcode_file="/data/Metadata/plate_1.gcode",
        current_print=subtask_name,
        awaiting_plate_clear=awaiting_plate_clear,
    )
