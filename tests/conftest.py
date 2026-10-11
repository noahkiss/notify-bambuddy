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
        # An iOS device by default, because that is what most of the suite is
        # about. The notifications-only tests override it.
        tiles_enabled=True,
        device_kind="iphone",
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
        # Layout defaults, matching what config.py produces with nothing set,
        # so every existing test keeps describing the default tile.
        tile_progress="bar",
        tile_segments=10,
        tile_metrics=False,
        tile_metric_fields=(),
        bambuddy_rich_status=False,
        alert_hms="serious",
        alert_offline=False,
        alert_plate_clear=False,
        alert_pause=False,
        tile_stage=False,
        tile_button_url=None,
        tile_button_title="Open",
        tile_button_open=True,
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

    #: When True, every started tile stays in the gateway's `starting` state
    #: instead of becoming `active`. That is what a real device does whenever
    #: the Notify app is closed, which for a background integration is most of
    #: the time: iOS shows the tile, but nothing reports back that it did.
    #: Tests that care about the verification poll set this.
    leave_unconfirmed: bool = False

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
            activity_id=activity_id,
            state="starting" if self.leave_unconfirmed else "active",
            end_reason=None,
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

    def plant_orphan(self, title: str, activity_id: str = None) -> str:
        """Put a live tile on the device that no session owns.

        Models the several ways one arrives: a start whose response was lost,
        a tile the gateway reaped while iOS kept showing it, or a state
        database thrown away with the container. The title is what the sweep
        matches on, so it is the part that matters.
        """
        self._counter += 1
        activity_id = activity_id or f"LAORPHAN{self._counter:02d}"
        self.states[activity_id] = ActivityStatus(
            activity_id=activity_id,
            state="active",
            end_reason=None,
            expires_at=None,
            raw={"activityId": activity_id, "state": "active",
                 "content": {"title": title}},
        )
        return activity_id

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


def build(config=None, notify=None, with_alerts: bool = False, store=None):
    """A coordinator wired to an in-memory store and a fake gateway.

    `store` and `notify` can be passed in to model a RESTART: a second
    coordinator over the same persisted state and the same gateway is exactly
    what the next process sees.
    """
    config = config or make_config()
    notify = notify or FakeNotify(max_tiles=config.max_tiles)
    store = store if store is not None else Store(":memory:")
    alerts = Alerts(config, notify) if with_alerts else None
    return Coordinator(config, store, notify, alerts=alerts), notify, store

#: One reading, in the shape the service can actually receive.
#:
#: LAYERS DEFAULT TO ZERO ON PURPOSE. `bambuddy_api.status_to_event` hardcodes
#: both layer fields to 0, because BambuBuddy's status route does not report
#: them. A fixture that supplied 10 and 300 meant every coordinator test ran
#: against a printer that does not exist, and that single default is why a
#: whole detector sat unreachable under a green suite: the tests fed it an
#: input production cannot produce, so it worked in tests and never once ran
#: in the field.
#:
#: Pass layer numbers explicitly only when testing the layer path itself.
#: They are reachable in production ONLY with BAMBUDDY_RICH_STATUS, which polls
#: BambuBuddy's printers route instead; on the webhook route they are still
#: hardcoded to zero. A test that supplies them is therefore describing a
#: rich-status deployment, and should say so.

def status(clock: Clock, *, serial: str = SERIAL, state: str = "RUNNING",
           progress: float = 10.0, remaining_minutes: int = 100,
           layer_num: int = 0, total_layers: int = 0, connected: bool = True,
           subtask_name: str = "Benchy", offset: float = 0.0,
           awaiting_plate_clear: bool = False, printer_name: str = "X1C-1",
           hms_errors: tuple = (), stage_name=None, temperatures=None,
           door_open: bool = False, rich=None) -> PrinterStatus:
    # `rich` marks the frame as having come from the printers route, which is
    # what licenses inferring "no faults" from an empty list. Defaulted from
    # the rich-only fields rather than required, because a test that supplies
    # faults or temperatures is describing a rich frame by definition, and
    # making every such test say so twice invites them to disagree.
    if rich is None:
        rich = bool(hms_errors or temperatures or stage_name)
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
        hms_errors=hms_errors,
        stage_name=stage_name,
        temperatures=temperatures,
        door_open=door_open,
        rich=rich,
    )
