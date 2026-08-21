# ---------------------------------------------------------------------------
# Regression tests.
#
# Every test here corresponds to a defect that an adversarial review of this
# codebase actually found and proved, by mutating the source and watching the
# rest of the suite stay green. They are kept together rather than scattered
# because their common lesson is worth seeing in one place: each of these bugs
# was invisible to a test suite that already covered the happy path.
#
# The service now has ONE input, a `PrinterStatus` frame from polling
# BambuBuddy's REST API, so every bug below that used to be provoked by a
# discrete lifecycle event is provoked by status frames instead. The bug each
# test guards is unchanged; only the way it is reached has moved.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import threading

import pytest
from conftest import SERIAL, FakeNotify, build, make_config, run, status

from notify_bambuddy import coordinator as coordinator_module
from notify_bambuddy.alerts import COOLDOWN_SECONDS, Alerts
from notify_bambuddy.coordinator import Coordinator
from notify_bambuddy.models import Outcome, Phase
from notify_bambuddy.notify_client import (
    ActivityStatus,
    NotifyAuthError,
    NotifyGone,
    NotifyTransportError,
)
from notify_bambuddy.store import Store


def end_print(coord, clock, *, state: str = "FINISH", serial: str = SERIAL, **fields):
    """Drive a print to its terminal outcome, the only way that now exists.

    There is no `print/finished` event any more. The poller produces status
    frames and nothing else, so a print ends when the printer has reported an
    inactive state for longer than `INACTIVE_GRACE_SECONDS` and the
    coordinator INFERS the outcome from it: FINISH becomes COMPLETED, FAILED
    becomes FAILED, and anything else inactive becomes ABORTED.

    Two frames, either side of the clock advance, because the grace is ARMED
    by the first inactive frame and only CHECKED on a later one. A tick will
    not do it: the inference lives in `_advance_phase`, which runs on an
    incoming frame and nowhere else.
    """
    run(coord.handle(status(clock, serial=serial, state=state, **fields)))
    clock.advance(coordinator_module.INACTIVE_GRACE_SECONDS + 1)
    run(coord.handle(status(clock, serial=serial, state=state, **fields)))


# ---------------------------------------------------------------------------
# Store: the SQLite connection is reached from two threads
# ---------------------------------------------------------------------------


def test_the_store_survives_concurrent_use_from_two_threads():
    """The store is one sqlite3 connection opened `check_same_thread=False`,
    so anything reaching it off the event loop interleaves with the loop's own
    writes.

    A shared connection shares its transaction state and its change counter.
    Without a lock this produced "cannot commit transaction", and worse, a
    read returning the wrong answer with no error at all. A dismissal read
    that says "not dismissed" a moment after it was recorded puts the tile
    the user just swiped straight back on their Lock Screen.
    """
    from test_store_and_config import make_session

    store = Store(":memory:")
    errors: list = []
    wrong_answers: list = []

    def read_from_another_thread() -> None:
        try:
            for i in range(400):
                serial = f"S{i:04d}"
                store.record_dismissal(serial)
                if not store.is_dismissed(serial):
                    wrong_answers.append(f"{serial} recorded but read back as absent")
                store.clear_dismissal(serial)
                if store.is_dismissed(serial):
                    wrong_answers.append(f"{serial} cleared but read back as present")
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch these
            errors.append(exc)

    def write_like_the_loop() -> None:
        try:
            for i in range(400):
                store.save_session(make_session(progress=float(i % 100)))
                if i % 50 == 0:
                    store.prune()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read_from_another_thread),
               threading.Thread(target=write_like_the_loop)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"concurrent access raised: {errors}"
    assert wrong_answers == [], f"the store answered wrongly under concurrency: {wrong_answers}"


# ---------------------------------------------------------------------------
# Sessions must never be left resident
# ---------------------------------------------------------------------------


def test_a_print_that_ends_without_a_tile_does_not_block_the_printer_forever(clock):
    """The nastiest of the lot, because it is permanent and silent.

    `_end_tile` used to return early when there was no tile to end, leaving a
    TERMINAL session parked on that serial. The status creation path only
    fires when there is NO session, and the terminal-sticky guard drops every
    frame for one that exists, so that printer could never get another tile for
    as long as the process ran.
    """
    coord, notify, _ = build(make_config(terminal_hold_s=0), FakeNotify(max_tiles=1))

    # Occupy the only slot with another printer, so this print never gets a tile.
    run(coord.handle(status(clock, serial="OTHER", printer_name="Other")))
    run(coord.handle(status(clock, serial=SERIAL)))
    assert coord._sessions[SERIAL].activity_id is None

    end_print(coord, clock)
    run(coord.tick())

    assert SERIAL not in coord._sessions, "the session must be retired, tile or no tile"
    assert SERIAL not in coord._waiting_for_slot

    # And the printer can start again.
    clock.advance(60)
    run(coord.handle(status(clock, serial=SERIAL, progress=5.0, layer_num=3)))
    assert SERIAL in coord._sessions


def test_a_new_print_replacing_a_session_does_not_orphan_the_old_tile(clock):
    """A session being replaced by its successor still owns a tile.

    Falling through and overwriting it in `_sessions` left that tile on the
    Lock Screen with nothing tracking it, until the gateway reaped it hours
    later. The way one print replaces another is a layer number that has gone
    BACKWARDS after a gap in telemetry, which is how a print started from the
    touchscreen while this service was not looking announces itself.
    """
    coord, notify, _ = build(make_config(silence_after_s=90))
    run(coord.handle(status(clock, layer_num=150)))
    first = notify.started[0]["activityId"]

    # A gap longer than the silence threshold, and then a layer count lower
    # than the one this session reached: a different print.
    clock.advance(120)
    run(coord.handle(status(clock, layer_num=2, progress=1.0)))

    assert first in [entry[0] for entry in notify.ended], "the old tile must be closed"
    assert len(notify.started) == 2
    assert coord._sessions[SERIAL].activity_id == notify.started[1]["activityId"]


# ---------------------------------------------------------------------------
# Silence has to end somewhere
# ---------------------------------------------------------------------------


def test_a_printer_that_never_comes_back_eventually_loses_its_tile(clock):
    """A printer switched off at the wall publishes nothing ever again, so no
    terminal state is coming. Without an exit from SILENT the keepalive would
    hold a "Connection lost" tile until Apple's eight-hour ceiling, and the
    rollover would then start a fresh one and do it again."""
    coord, notify, _ = build(make_config(
        silence_after_s=90, abandon_after_s=600, terminal_hold_s=0,
    ))
    run(coord.handle(status(clock)))

    clock.advance(120)
    run(coord.tick())
    assert coord._sessions[SERIAL].phase is Phase.SILENT
    assert notify.ended == [], "a brief silence must not end the tile"

    clock.advance(600)
    run(coord.tick())   # notices the silence and writes the terminal outcome
    run(coord.tick())   # the hold (zero here) expires and the tile is ended
    assert len(notify.ended) == 1
    assert SERIAL not in coord._sessions


# ---------------------------------------------------------------------------
# Failure handling that used to hot-loop or give up
# ---------------------------------------------------------------------------


def test_a_transport_failure_backs_off_instead_of_retrying_every_tick(clock):
    """With the gateway unreachable every attempt costs a full HTTP timeout,
    so an unbacked-off retry queue would block the event loop every tick while
    polled frames piled up behind it."""
    coord, notify, _ = build()
    notify.fail_next("start", NotifyTransportError("connection refused"))
    run(coord.handle(status(clock)))
    assert notify.started == []
    assert SERIAL in coord._waiting_for_slot

    clock.advance(5)
    run(coord.tick())
    assert notify.started == [], "it must not retry immediately"

    clock.advance(coordinator_module.SERVICE_BACKOFF_SECONDS + 1)
    run(coord.tick())
    assert len(notify.started) == 1, "it must retry once the backoff expires"


def test_a_rejected_credential_does_not_abandon_a_running_print(clock):
    """This path is reachable mid-rollover, where dropping the session would
    leave a running print with no tile for the rest of its life."""
    coord, notify, _ = build()
    notify.fail_next("start", NotifyAuthError("Invalid token or id not found", 403))
    run(coord.handle(status(clock)))
    assert notify.started == []
    assert SERIAL in coord._waiting_for_slot

    clock.advance(coordinator_module.SERVICE_BACKOFF_SECONDS + 1)
    run(coord.tick())
    assert len(notify.started) == 1


def test_a_transient_error_does_not_permanently_disarm_start_verification(clock):
    """The check that catches silently discarded tiles used to drop its own
    schedule entry BEFORE calling the gateway, so one blip disabled it."""
    coord, notify, _ = build(make_config(start_verify_after_s=90))
    run(coord.handle(status(clock)))
    activity_id = notify.started[0]["activityId"]
    notify.never_started(activity_id)

    from notify_bambuddy.notify_client import NotifyError
    notify.fail_next("status", NotifyError("upstream hiccup", 500))

    clock.advance(91)
    run(coord.tick())
    assert coord.snapshot()["unconfirmed_starts"] == 0, \
        "the failed check should not have concluded anything"

    run(coord.tick())
    assert coord.snapshot()["unconfirmed_starts"] == 1, \
        "the check must run again rather than stay disarmed"
    assert activity_id not in [entry[0] for entry in notify.ended], \
        "and it must not end a tile nothing is waiting for"


def test_verification_never_revives_a_finished_print(clock):
    """A finished print inside its hold must not have a brand new tile pushed
    for it because the old one never became visible."""
    coord, notify, _ = build(make_config(start_verify_after_s=30, terminal_hold_s=300))
    run(coord.handle(status(clock)))
    notify.never_started(notify.started[0]["activityId"])
    end_print(coord, clock)

    clock.advance(31)
    run(coord.tick())
    assert len(notify.started) == 1, "no successor tile for a finished print"


def test_the_tile_cap_branch_is_actually_exercised(clock):
    """The gateway reports its 5-tile cap as a 400, the one transient 400 in
    the API. A client that treats it as permanent writes off a printer."""
    coord, notify, _ = build(make_config(max_tiles=5), FakeNotify(max_tiles=1))
    run(coord.handle(status(clock, serial="A", printer_name="A")))
    run(coord.handle(status(clock, serial="B", printer_name="B")))

    assert len(notify.started) == 1
    assert "B" in coord._waiting_for_slot, "the capped printer waits rather than failing"


def test_a_freed_slot_prefers_a_running_print_over_a_paused_one(clock):
    """A paused print's ETA is frozen at whatever it said when it stopped
    moving, so ranking on ETA alone lets it jump ahead of a running print that
    is genuinely closer to done."""
    # terminal_hold_s=0 so the slot is freed on the first tick after the
    # holder's outcome is written.
    coord, notify, _ = build(make_config(max_tiles=1, pause_suppress_s=0,
                                         terminal_hold_s=0),
                             FakeNotify(max_tiles=1))
    run(coord.handle(status(clock, serial="HOLDER", printer_name="Holder")))

    # Paused, with a very short frozen ETA.
    run(coord.handle(status(clock, serial="PAUSED", printer_name="Paused",
                            remaining_minutes=1, state="PAUSE")))
    # Running, further from finishing.
    run(coord.handle(status(clock, serial="RUNNING", printer_name="Running",
                            remaining_minutes=60)))
    assert sorted(coord._waiting_for_slot) == ["PAUSED", "RUNNING"]

    # The holder finishes. Inferring that takes longer than the silence
    # threshold, so the two waiting printers have to keep reporting across the
    # wait: a session marked SILENT ranks alongside a paused one and would
    # defeat the very comparison under test.
    run(coord.handle(status(clock, serial="HOLDER", printer_name="Holder",
                            state="FINISH")))
    clock.advance(coordinator_module.INACTIVE_GRACE_SECONDS + 1)
    run(coord.handle(status(clock, serial="PAUSED", printer_name="Paused",
                            remaining_minutes=1, state="PAUSE")))
    run(coord.handle(status(clock, serial="RUNNING", printer_name="Running",
                            remaining_minutes=60)))
    run(coord.handle(status(clock, serial="HOLDER", printer_name="Holder",
                            state="FINISH")))
    run(coord.tick())

    assert coord._sessions["RUNNING"].activity_id is not None, \
        "the running print should win the freed slot"
    assert coord._sessions["PAUSED"].activity_id is None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_privacy_mode_keeps_the_job_name_out_of_alerts(clock):
    coord, notify, _ = build(make_config(privacy_mode=True), with_alerts=True)
    run(coord.handle(status(clock, subtask_name="Secret Project")))
    end_print(coord, clock, subtask_name="Secret Project")
    assert notify.notifications
    for alert in notify.notifications:
        assert "Secret Project" not in alert["text"]


def test_a_repeated_alert_of_the_same_kind_is_suppressed():
    """A flapping link bounces a printer in and out of silence, and every
    bounce would otherwise be its own notification."""
    config = make_config()
    notify = FakeNotify()
    alerts = Alerts(config, notify)

    run(alerts.printer_silent(SERIAL, "X1C-1", "Benchy"))
    run(alerts.printer_silent(SERIAL, "X1C-1", "Benchy"))
    assert len(notify.notifications) == 1

    # A different printer is a different alert.
    run(alerts.printer_silent("OTHER", "A1", "Benchy"))
    assert len(notify.notifications) == 2
    assert COOLDOWN_SECONDS > 0


def test_a_failed_alert_never_propagates_out_of_the_state_machine(clock):
    """The tile is the primary channel. A notification that could not be sent
    must not become an exception the coordinator has to handle mid-transition."""
    from notify_bambuddy.notify_client import NotifyError

    coord, notify, _ = build(with_alerts=True)
    run(coord.handle(status(clock)))
    notify.fail_next("notify", NotifyError("gateway sulking", 500))

    end_print(coord, clock)
    # The tile still got its terminal content despite the alert failing.
    assert notify.updates[-1][1]["status"] == "Done"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_a_group_credential_is_rejected_at_startup_with_an_explanation():
    """Without this check the Live Activity endpoints would answer a group with
    the same uniform 403 they use for a bad token, telling the user nothing."""
    from notify_bambuddy.config import ConfigError
    from notify_bambuddy.__main__ import validate_credentials

    notify = FakeNotify()
    notify.link_response = {"success": True, "type": "group", "id": "GRP12345"}
    with pytest.raises(ConfigError) as caught:
        run(validate_credentials(notify))
    assert "GROUP" in str(caught.value)


def test_valid_device_credentials_pass_the_startup_check():
    from notify_bambuddy.__main__ import validate_credentials

    notify = FakeNotify()
    run(validate_credentials(notify))  # must not raise


def test_the_health_snapshot_never_contains_the_token(clock):
    """It exists to be pasted into issue reports."""
    coord, notify, _ = build(with_alerts=True)
    run(coord.handle(status(clock)))
    rendered = json.dumps(coord.snapshot(), default=str)
    assert make_config().device_token not in rendered


def test_the_health_endpoint_serves_its_three_paths(clock):
    import urllib.request

    from notify_bambuddy.health import HealthServer

    coord, notify, _ = build()
    run(coord.handle(status(clock)))

    server = HealthServer("127.0.0.1", 0, lambda: {
        "source": {"source": "api", "polls_succeeded": 3, "consecutive_failures": 0},
        **coord.snapshot(),
    })
    # Bind to an ephemeral port, then read back which one we got.
    port = server._server.server_address[1]
    server.start()
    try:
        for path, expected in [("/healthz", 200), ("/status", 200), ("/nope", 404)]:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=5
                ) as response:
                    assert response.status == expected
                    body = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                assert exc.code == expected
                continue
            if path == "/status":
                assert body["tile_limit"] == 5
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Second audit round
# ---------------------------------------------------------------------------


def test_a_traceback_cannot_leak_the_token(caplog):
    """`Formatter.format` renders `exc_info` into `exc_text` only AFTER filters
    have run, then reuses `exc_text` if already set. A filter that only scrubs
    `exc_text` therefore never sees the traceback. The fix pre-renders it."""
    import logging as _logging
    from notify_bambuddy.redact import PLACEHOLDER, RedactingFilter

    token = "XYZ789TOKEN123"
    logger = _logging.getLogger("leaktest")
    logger.propagate = False
    stream = []

    class Capture(_logging.Handler):
        def emit(self, record):
            stream.append(self.format(record))

    handler = Capture()
    handler.setFormatter(_logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter([token]))
    logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)
    try:
        try:
            raise RuntimeError(f"failed: https://host/live-activity/X?token={token}")
        except RuntimeError:
            logger.exception("boom")
        # A non-string lazy argument whose str() carries the token.
        logger.error("request failed: %s", RuntimeError(f"token={token}"))
    finally:
        logger.removeHandler(handler)

    joined = "\n".join(stream)
    assert token not in joined
    assert PLACEHOLDER in joined
    assert "Traceback" in joined, "the traceback itself must still be logged"


def test_http_client_exceptions_become_transport_errors(monkeypatch):
    """`BadStatusLine`, `RemoteDisconnected` and friends are not `OSError`
    subclasses, so without an explicit mapping they escape every
    `except NotifyError` in the coordinator."""
    import http.client
    import urllib.request
    from notify_bambuddy.notify_client import NotifyClient, NotifyTransportError

    def boom(*args, **kwargs):
        raise http.client.RemoteDisconnected("Remote end closed connection")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    client = NotifyClient("http://127.0.0.1:1", "ABC12345", "XYZ789TOKEN123", timeout=1.0)
    with pytest.raises(NotifyTransportError):
        run(client.update("LA000001", {"progress": 1}))


def test_dry_run_never_asks_the_real_gateway_about_synthetic_tiles(monkeypatch):
    """A dry run used to hand out one constant id and then verify it against
    the real gateway every tick, forever, with a real token in the query."""
    import urllib.request
    from notify_bambuddy.notify_client import NotifyClient

    calls = []

    def record(request, *args, **kwargs):
        calls.append(request.full_url)
        raise AssertionError("the network must not be touched for a synthetic tile")

    monkeypatch.setattr(urllib.request, "urlopen", record)
    client = NotifyClient("https://notifypush.example", "ABC12345", "XYZ789TOKEN123",
                          dry_run=True)
    started = run(client.start({"title": "X1C-1", "progress": 0}))
    assert started.activity_id.startswith("LADRY")
    activity_status = run(client.status(started.activity_id))
    assert activity_status.is_confirmed
    listed = run(client.list_device())
    assert [a.activity_id for a in listed] == [started.activity_id]
    run(client.end(started.activity_id, None, 0))
    assert not run(client.list_device())
    assert calls == []


def test_a_lost_start_response_adopts_the_orphan_instead_of_starting_twice(clock):
    """The gateway may have created the tile and lost only the response. The
    retry must look for it first, or a print ends up with two tiles."""
    coord, notify, _ = build()

    # The first attempt "times out" AFTER the fake gateway recorded a tile.
    real_start = notify.start

    async def start_then_timeout(content):
        await real_start(content)
        raise NotifyTransportError("timed out")

    notify.start = start_then_timeout
    run(coord.handle(status(clock)))
    assert len(notify.started) == 1, "the gateway has a tile this service does not know about"
    assert coord._sessions[SERIAL].activity_id is None
    assert coord._sessions[SERIAL].start_ambiguous

    notify.start = real_start
    clock.advance(coordinator_module.GATEWAY_BACKOFF_SECONDS + 1)
    run(coord.tick())

    assert len(notify.started) == 1, "no second tile was created"
    assert coord._sessions[SERIAL].activity_id == notify.started[0]["activityId"], \
        "the orphan was adopted"


def test_a_restart_after_long_downtime_does_not_abort_a_live_print(clock):
    """Downtime is not silence. The stored `last_status_ts` is hours old after
    a long restart, and without resetting it the first tick would mark every
    running print abandoned."""
    coord, notify, store = build(make_config(abandon_after_s=600))
    run(coord.handle(status(clock)))

    clock.advance(3600)  # the service was down for an hour
    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())
    run(reborn.tick())

    assert notify.ended == [], "the print is still running; nothing is known to the contrary"
    assert reborn._sessions[SERIAL].phase is not Phase.TERMINAL


def test_recovery_waits_for_live_telemetry_before_giving_a_tile_to_a_tile_less_print(clock):
    """What the store remembers is that a print WAS running when the process
    stopped, which may have been days ago. Queuing it immediately would create
    a tile for a print that finished during the outage. It is retired, and the
    normal creation path rebuilds it from a live frame."""
    coord, notify, store = build(make_config(max_tiles=1, running_grace_s=0),
                                 FakeNotify(max_tiles=1))
    run(coord.handle(status(clock, serial="HOLDER", printer_name="Holder")))
    run(coord.handle(status(clock, serial=SERIAL)))
    assert coord._sessions[SERIAL].activity_id is None

    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())
    assert SERIAL not in reborn._waiting_for_slot
    assert SERIAL not in reborn._sessions, "nothing is assumed about a tile-less print"

    # The print really is still running, and says so. Now it gets a tile.
    clock.advance(30)
    run(reborn.handle(status(clock, serial="HOLDER", printer_name="Holder")))
    run(reborn.handle(status(clock, serial=SERIAL)))
    assert SERIAL in reborn._sessions


def test_recovery_adopts_a_tile_from_a_start_that_was_interrupted_mid_flight(clock):
    """The process died between the POST and the response. The intent was
    written down first, so the next run reconciles instead of starting a
    second tile for the same print."""
    coord, notify, store = build()

    real_start = notify.start

    async def start_then_die(content):
        await real_start(content)                 # the gateway made the tile
        raise KeyboardInterrupt("process killed mid-start")

    notify.start = start_then_die
    try:
        run(coord.handle(status(clock)))
    except KeyboardInterrupt:
        pass
    assert len(notify.started) == 1
    stored = store.get_session(coord._sessions[SERIAL].session_key)
    assert stored is not None and stored.activity_id is None

    notify.start = real_start
    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())

    assert len(notify.started) == 1, "recovery must not create a second tile"
    assert reborn._sessions[SERIAL].activity_id == notify.started[0]["activityId"]


def test_an_end_that_times_out_is_retried_rather_than_assumed(clock):
    """Treating a timed-out DELETE as success clears the activity id, leaving
    a tile live on the Lock Screen that nothing can ever end or update."""
    coord, notify, _ = build(make_config(terminal_hold_s=0))
    run(coord.handle(status(clock)))
    activity_id = notify.started[0]["activityId"]

    real_end = notify.end
    notify.end = lambda *a, **k: (_ for _ in ()).throw(NotifyTransportError("timed out"))
    end_print(coord, clock)
    run(coord.tick())
    assert coord._sessions[SERIAL].activity_id == activity_id, "ownership must be kept"
    assert coord._sessions[SERIAL].delivery.value == "ending"

    notify.end = real_end
    clock.advance(coordinator_module.GATEWAY_BACKOFF_SECONDS + 1)
    run(coord.tick())
    assert activity_id in [entry[0] for entry in notify.ended]
    assert SERIAL not in coord._sessions


def test_a_finished_print_clears_the_dismissal_the_moment_its_outcome_is_written(clock):
    """A dismissal dies with the print it belonged to.

    Not when the tile is eventually ended, which can be minutes later: the
    dismissal is spent as soon as the outcome is decided, or a swipe would
    outlive its print by the whole dismissal window and silence the next one.
    """
    coord, notify, store = build(make_config(terminal_hold_s=120))
    run(coord.handle(status(clock)))
    notify.dismiss(notify.started[0]["activityId"])
    clock.advance(20)
    run(coord.handle(status(clock, progress=20.0, layer_num=20)))
    assert coord._sessions[SERIAL].delivery.value == "dismissed"

    end_print(coord, clock, progress=20.0, layer_num=20)
    assert coord._sessions[SERIAL].outcome is Outcome.COMPLETED
    assert not store.is_dismissed(SERIAL), "the dismissal is spent with its print"

    # The hold expires and the parked session goes, tile or no tile.
    clock.advance(121)
    run(coord.tick())
    assert SERIAL not in coord._sessions

    # And the next print gets a tile.
    clock.advance(600)
    run(coord.handle(status(clock, progress=1.0, layer_num=1)))
    assert len(notify.started) == 2


def test_a_gateway_outage_does_not_cost_a_timeout_per_frame(clock):
    """During an outage every paced update used to block the loop for a full
    HTTP timeout. One failure now backs the whole device off briefly."""
    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock)))
    attempts = []
    real_update = notify.update

    async def flaky(activity_id, content):
        attempts.append(1)
        raise NotifyTransportError("connection refused")

    notify.update = flaky
    for i in range(5):
        clock.advance(1)
        run(coord.handle(status(clock, progress=20.0 + i, layer_num=30 + i)))
    assert len(attempts) == 1, "only the first frame pays the timeout; the rest wait"

    notify.update = real_update
    clock.advance(coordinator_module.GATEWAY_BACKOFF_SECONDS + 1)
    run(coord.handle(status(clock, progress=30.0, layer_num=40)))
    assert notify.updates, "updates resume once the backoff expires"


def test_a_terminal_alert_is_not_swallowed_by_the_previous_prints_cooldown(clock):
    """The cooldown is per printer and kind. Two prints ending within fifteen
    minutes on one machine are two pieces of news, not a repeat."""
    coord, notify, _ = build(make_config(terminal_hold_s=0), with_alerts=True)
    for n in range(2):
        run(coord.handle(status(clock, layer_num=10, subtask_name=f"Job {n}")))
        end_print(coord, clock, subtask_name=f"Job {n}")
        run(coord.tick())
        clock.advance(120)
    assert len(notify.notifications) == 2


def test_the_layer_counter_is_not_copied_from_an_idle_frame(clock):
    """A FINISH or IDLE frame carries zeros. Copying them in would make the
    next RUNNING frame look like a layer reset."""
    coord, notify, _ = build()
    run(coord.handle(status(clock, layer_num=150)))
    run(coord.handle(status(clock, state="IDLE", layer_num=0, total_layers=0)))
    assert coord._sessions[SERIAL].layer_num == 150
    assert coord._sessions[SERIAL].total_layers == 300


def test_the_migration_adds_missing_columns_to_an_older_database(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that already
    exists, so a database written by an earlier build needs its new columns
    added explicitly or every write fails."""
    import sqlite3

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE sessions (
            session_key TEXT PRIMARY KEY, serial TEXT NOT NULL, printer_name TEXT,
            phase TEXT NOT NULL, delivery TEXT NOT NULL, activity_id TEXT,
            expires_at REAL, job_name TEXT, job_id INTEGER,
            progress REAL NOT NULL DEFAULT 0, eta_seconds INTEGER,
            layer_num INTEGER NOT NULL DEFAULT 0, total_layers INTEGER NOT NULL DEFAULT 0,
            outcome TEXT, terminal_at REAL, last_source_ts REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0, last_sent_at REAL NOT NULL DEFAULT 0,
            last_payload_hash TEXT, progress_trustworthy INTEGER NOT NULL DEFAULT 0,
            progress_at_detection REAL NOT NULL DEFAULT 0,
            last_status_ts REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
        );
    """)
    old.commit()
    old.close()

    from test_store_and_config import make_session

    store = Store(path)
    store.save_session(make_session(session_key="k", serial="S"))
    loaded = store.get_session("k")
    assert loaded is not None and loaded.start_ambiguous is False
    store.close()


# ---------------------------------------------------------------------------
# Third audit round
# ---------------------------------------------------------------------------


def test_a_dismissed_print_that_ends_silently_does_not_block_the_printer(clock):
    """The trap: one swipe plus one dropped terminal state silenced a printer
    for good.

    A DISMISSED session used to return before `_advance_phase`, so the
    inactive grace could never arm for it and the session sat in `_sessions`
    forever. With a session present, the status creation path is unreachable,
    so no later print could get a tile either.
    """
    coord, notify, store = build(make_config(terminal_hold_s=0, silence_after_s=90,
                                             progress_min_interval_s=0))
    run(coord.handle(status(clock, progress=80.0, layer_num=300)))
    notify.dismiss(notify.started[0]["activityId"])
    clock.advance(20)
    run(coord.handle(status(clock, progress=81.0, layer_num=301)))
    assert coord._sessions[SERIAL].delivery.value == "dismissed"

    # Print A ends. The printer simply reports FINISH from now on.
    for _ in range(6):
        clock.advance(30)
        run(coord.handle(status(clock, state="FINISH", progress=81.0, layer_num=301)))
        run(coord.tick())

    assert SERIAL not in coord._sessions, "the inactive grace must retire it"
    assert not store.is_dismissed(SERIAL), "the dismissal died with its print"

    # Print B, started from the touchscreen.
    clock.advance(30)
    run(coord.handle(status(clock, state="RUNNING", progress=1.0, layer_num=1)))
    assert len(notify.started) == 2, "the next print gets a tile"


def test_a_disconnected_frame_does_not_keep_the_tile_looking_alive(clock):
    """BambuBuddy keeps reporting `connected: false` while a printer is
    unreachable. Counting those as telemetry meant the tile never reached
    "Connection lost" and was never abandoned."""
    coord, notify, _ = build(make_config(silence_after_s=90, abandon_after_s=600))
    run(coord.handle(status(clock)))

    for _ in range(10):
        clock.advance(30)
        run(coord.handle(status(clock, connected=False)))
        run(coord.tick())

    assert coord._sessions[SERIAL].phase is Phase.SILENT, \
        "a stream of disconnected frames is silence, not life"


def test_privacy_mode_keeps_the_job_name_out_of_the_health_snapshot(clock):
    """Privacy mode has to cover every surface the name can escape by, not
    only the tile. The health snapshot exists to be pasted into issue
    reports, so a model name left in it is the same leak as one photographed
    off a Lock Screen."""
    coord, notify, _ = build(make_config(privacy_mode=True))
    run(coord.handle(status(clock, subtask_name="Secret Prototype")))
    assert coord._sessions[SERIAL].job_name == "Secret Prototype", \
        "the name is tracked; it is the reporting of it that is suppressed"

    rendered = json.dumps(coord.snapshot(), default=str)
    assert "Secret Prototype" not in rendered


def test_an_unreadable_410_decides_nothing_and_asks_again(clock):
    """Both defaults are wrong: assume dismissal and a reaped tile never comes
    back; assume reaping and a swiped tile reappears."""
    from notify_bambuddy.notify_client import NotifyError

    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock)))
    activity_id = notify.started[0]["activityId"]
    notify.reap(activity_id, reason="overdue")
    notify.fail_next("status", NotifyError("gateway sulking", 500))

    clock.advance(20)
    run(coord.handle(status(clock, progress=30.0, layer_num=30)))
    assert len(notify.started) == 1, "nothing is decided while the reason is unknown"
    assert coord._sessions[SERIAL].pending_gone

    # The gateway recovers and answers; now the tile is recreated.
    clock.advance(20)
    run(coord.tick())
    assert len(notify.started) == 2


def test_a_future_dated_frame_cannot_freeze_the_session(clock):
    """A publisher whose clock is an hour fast would set `last_source_ts` an
    hour ahead, and every real frame after it would be dropped as stale."""
    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock, progress=10.0, layer_num=10)))

    run(coord.handle(status(clock, progress=99.0, layer_num=310, offset=3600)))
    assert coord._sessions[SERIAL].progress == 10.0, "the future frame was ignored"

    clock.advance(30)
    run(coord.handle(status(clock, progress=12.0, layer_num=14)))
    assert coord._sessions[SERIAL].progress == 12.0, "real frames still apply"


# ---------------------------------------------------------------------------
# Fourth audit round: the crash-durability code itself
# ---------------------------------------------------------------------------


def test_a_failed_start_does_not_strand_the_tile_without_a_countdown(clock):
    """`build_content` used to record the countdown as sent while merely
    BUILDING. A start whose POST then failed left the session believing a
    countdown was on a tile that did not exist, and because the recorded value
    decays at the same rate as the printer's estimate, the two agreed forever
    and `endsIn` was never sent again."""
    coord, notify, _ = build()
    notify.fail_next("start", NotifyTransportError("connection refused"))
    run(coord.handle(status(clock, remaining_minutes=120)))
    assert notify.started == []
    assert coord._sessions[SERIAL].sent_eta_seconds is None, "nothing reached a tile"

    clock.advance(coordinator_module.GATEWAY_BACKOFF_SECONDS + 1)
    run(coord.handle(status(clock, remaining_minutes=118, progress=12.0, layer_num=14)))
    run(coord.tick())

    assert len(notify.started) == 1
    assert notify.started[0]["content"]["endsIn"] == 118 * 60, \
        "the retried start must still carry a countdown"


def test_a_suppressed_update_does_not_lose_a_revised_eta(clock):
    """The same bug through the pacing floor: the printer revises its estimate,
    the update is held back for fifteen seconds, and the revision is then
    never delivered at all."""
    coord, notify, _ = build(make_config(progress_min_interval_s=15))
    run(coord.handle(status(clock, remaining_minutes=120)))
    assert notify.started[0]["content"]["endsIn"] == 7200
    notify.updates.clear()

    # Five seconds later the printer revises to five hours. Held by the floor.
    clock.advance(5)
    run(coord.handle(status(clock, remaining_minutes=300, progress=11.0, layer_num=12)))
    assert notify.updates == []

    # Past the floor, the revision must actually arrive.
    clock.advance(20)
    run(coord.handle(status(clock, remaining_minutes=299, progress=12.0, layer_num=13)))
    delivered = [c.get("endsIn") for _, c in notify.updates if "endsIn" in c]
    assert delivered, "the revised estimate was lost"
    assert delivered[-1] == 299 * 60


def test_an_unchanged_estimate_is_still_not_resent(clock):
    """The other half: `endsIn` is relative, so resending it pushes the finish
    later. The purity fix must not turn into "resend every time"."""
    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock, remaining_minutes=100)))
    notify.updates.clear()

    for _ in range(4):
        clock.advance(20)
        run(coord.handle(status(clock, remaining_minutes=100, progress=20.0, layer_num=25)))
    resent = [c for _, c in notify.updates if "endsIn" in c]
    assert resent == [], "an unchanged estimate must not be rewritten"


def test_an_end_owed_survives_the_next_print_evicting_its_session(clock):
    """`_sessions` holds one session per printer, so a new print evicts the one
    whose tile still needs ending. Driving the retry off `_sessions` meant that
    tile was never ended at all."""
    coord, notify, _ = build(make_config(terminal_hold_s=0, silence_after_s=90))
    run(coord.handle(status(clock, layer_num=150)))
    stranded = notify.started[0]["activityId"]

    notify.end = lambda *a, **k: (_ for _ in ()).throw(NotifyTransportError("timed out"))

    # A gap in telemetry, then a layer count that has gone backwards: a new
    # print, which takes the session slot while the old tile is still owed
    # an ending.
    clock.advance(120)
    run(coord.handle(status(clock, layer_num=2, progress=1.0)))
    assert stranded in coord._ends_owed
    assert coord._sessions[SERIAL].activity_id != stranded

    # The gateway comes back. The stranded tile must still be ended.
    from conftest import FakeNotify as _FakeNotify
    notify.end = _FakeNotify.end.__get__(notify, _FakeNotify)
    clock.advance(coordinator_module.GATEWAY_BACKOFF_SECONDS + 1)
    run(coord.tick())
    assert stranded in [entry[0] for entry in notify.ended]
    assert stranded not in coord._ends_owed


def test_a_restart_does_not_finalise_a_provisional_terminal_on_the_first_tick(clock):
    """After a restart there is no telemetry yet, which is indistinguishable
    from a printer that went quiet unless you look at how long the process has
    been up. Finalising on that would end a live print's tile immediately.

    The provisional flag round-trips through the store, so a restart is
    exactly where a session carrying one is met. It is set directly here
    because the flag is what is under test, not the route that raised it.
    """
    coord, notify, store = build(make_config(terminal_hold_s=0, silence_after_s=90))
    run(coord.handle(status(clock)))

    session = coord._sessions[SERIAL]
    session.outcome = Outcome.ABORTED
    session.phase = Phase.TERMINAL
    session.terminal_at = clock.time()
    session.terminal_provisional = True
    store.save_session(session)

    reborn = Coordinator(coord._config, store, notify, alerts=None)
    reborn._started_at = clock.time()
    run(reborn.recover())
    assert reborn._sessions[SERIAL].terminal_provisional, "the flag must survive the store"

    run(reborn.tick())
    assert notify.ended == [], "a provisional abort must survive the first tick after a restart"

    # And the print proves it is alive.
    clock.advance(10)
    run(reborn.handle(status(clock, progress=50.0, layer_num=160)))
    assert reborn._sessions[SERIAL].phase is Phase.PRINTING


def test_a_paused_print_does_not_finalise_a_provisional_abort(clock):
    """A pause is still a print in flight. Treating only RUNNING as alive
    would finalise an abort for a print sitting on a filament change.

    As above, the provisional flag is set directly: the hold's handling of it
    is the behaviour under test.
    """
    coord, notify, _ = build(make_config(terminal_hold_s=60, silence_after_s=90,
                                         pause_suppress_s=0))
    run(coord.handle(status(clock)))
    session = coord._sessions[SERIAL]
    session.outcome = Outcome.ABORTED
    session.phase = Phase.TERMINAL
    session.terminal_at = clock.time()
    session.terminal_provisional = True

    for _ in range(4):
        clock.advance(30)
        run(coord.handle(status(clock, state="PAUSE")))
        run(coord.tick())
    assert notify.ended == [], "a paused printer is still confirming the print"


def test_recovery_without_a_device_list_does_not_park_a_tile_less_session(clock):
    """When `list_device` fails there is no way to reconcile. Re-adopting a
    tile-less session parks it in `_sessions` with no tile and no path to one,
    which blocks the creation path for that printer entirely."""
    from notify_bambuddy.notify_client import NotifyError

    coord, notify, store = build(make_config(max_tiles=1, running_grace_s=0),
                                 FakeNotify(max_tiles=1))
    run(coord.handle(status(clock, serial="HOLDER", printer_name="Holder")))
    run(coord.handle(status(clock, serial=SERIAL)))
    assert coord._sessions[SERIAL].activity_id is None

    reborn = Coordinator(coord._config, store, notify, alerts=None)
    notify.fail_next("list_device", NotifyError("gateway down", 503))
    run(reborn.recover())
    assert SERIAL not in reborn._sessions, "a tile-less session must not be parked"

    clock.advance(30)
    run(reborn.handle(status(clock, serial=SERIAL)))
    assert SERIAL in reborn._sessions, "and the printer can start again"


def test_a_tile_dismissed_after_its_print_finished_does_not_silence_the_next(clock):
    """A swipe after the print ended carries no instruction about the next one."""
    coord, notify, store = build(make_config(terminal_hold_s=600))
    run(coord.handle(status(clock)))
    end_print(coord, clock)
    notify.dismiss(notify.started[0]["activityId"])

    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())
    assert not store.is_dismissed(SERIAL), "the print was already over"


def test_an_adopted_orphan_is_not_abandoned_on_the_first_tick(clock):
    """Adoption used to bypass `_readopt`, leaving the liveness clock at
    whatever the store remembered, so the abandon sweep ended it at once."""
    coord, notify, store = build(make_config(abandon_after_s=600))

    real_start = notify.start

    async def start_then_die(content):
        await real_start(content)
        raise KeyboardInterrupt("killed mid-start")

    notify.start = start_then_die
    try:
        run(coord.handle(status(clock)))
    except KeyboardInterrupt:
        pass
    notify.start = real_start

    clock.advance(3600)          # the service was down for an hour
    reborn = Coordinator(coord._config, store, notify, alerts=None)
    reborn._started_at = clock.time()
    run(reborn.recover())
    assert reborn._sessions[SERIAL].activity_id == notify.started[0]["activityId"]

    run(reborn.tick())
    run(reborn.tick())
    assert notify.ended == [], "the adopted tile must not be abandoned immediately"
    assert reborn._sessions[SERIAL].phase is not Phase.TERMINAL, \
        "downtime is not silence: the liveness clock must be reset on adoption"


def test_privacy_mode_keeps_the_job_name_out_of_the_start_log(clock, caplog):
    """Privacy mode is about the model name not being visible, and a log file
    is at least as likely to be shared as a Lock Screen photo. The start line
    is the one log line that always carries the job name, so it is the one
    that must run through the filter."""
    import logging

    coord, _, _ = build(make_config(privacy_mode=True))
    with caplog.at_level(logging.INFO):
        run(coord.handle(status(clock, subtask_name="Secret Prototype")))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "Print started on" in joined, "the start line itself should still log"
    assert "Secret Prototype" not in joined, "privacy mode leaked the job name to the log"


def test_a_never_started_reap_does_not_create_a_second_tile(clock):
    """Regression, from a real print on real hardware: two tiles on one phone.

    The gateway reaped a tile as `never-started`, which fell into the generic
    "the gateway tidied this away, recreate it" branch. But the tile was on
    the phone the whole time: a tile iOS displayed while the app stayed closed
    is never reported back, and looks exactly like one that never arrived. The
    replacement then sat alongside the original.

    That reap is the one that must not be retried. If the tile really never
    appeared a replacement hits the same wall; if it did, a replacement
    duplicates it.
    """
    coord, notify, _ = build()
    run(coord.handle(status(clock)))
    session = coord._sessions[SERIAL]
    first = session.activity_id
    assert first, "no tile was started"

    notify.states[first] = ActivityStatus(
        activity_id=first, state="ended", end_reason="never-started", expires_at=None, raw={})
    run(coord._handle_gone(session, NotifyGone("gone", 410, {}), clock.t))

    started_after = [s for s in notify.started if s["activityId"] != first]
    assert not started_after, "a second tile was created alongside one already on screen"
    assert session.activity_id is None
