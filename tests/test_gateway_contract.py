"""Behaviour pinned to the Notify gateway's own source, not to its docs.

Each of these was found by reading the gateway (Notify's notifyapns/server.js
and live-activity-prune.js) and comparing it with what this service assumed.
Every assumption here was wrong in a way a user could see.
"""

import logging

from notify_bambuddy.content import build_content, terminal_content
from notify_bambuddy.models import Delivery, Outcome, Phase, SessionState
from notify_bambuddy.notify_client import (
    ActivityStatus,
    NotifyDeliveryFailed,
    NotifyGone,
    NotifyRateLimited,
    NotifyUpstreamError,
)

from conftest import SERIAL, build, make_config, run, status


def _gone(notify, activity_id, reason):
    notify.states[activity_id] = ActivityStatus(
        activity_id=activity_id, state="ended", end_reason=reason,
        expires_at=None, raw={"activityId": activity_id, "endReason": reason},
    )
    notify.fail_next("update", NotifyGone("This Live Activity has ended", 410))


# --- 502 on a start ----------------------------------------------------------


def test_an_unknown_delivery_is_followed_not_forgotten(clock):
    """Apple never answered, so the tile may be appearing. The gateway kept
    its row and returned its id. Discarding it left the row unowned for the
    pre-start sweep to end later, possibly after the tile had appeared."""
    coord, notify, _ = build()
    notify.fail_next("start", NotifyUpstreamError(
        "Apple could not be reached and delivery is unknown.", 502,
        {"deliveryState": "unknown", "activityId": "LAUNKNWN"},
    ))
    run(coord.handle(status(clock)))
    session = coord._sessions[SERIAL]
    assert session.activity_id == "LAUNKNWN", "the tile that may exist was thrown away"
    assert session.delivery is Delivery.ACTIVE
    assert "LAUNKNWN" in coord._verify_at, "it must be checked like any other start"
    assert coord.snapshot()["starts_blocked_for"] == 0


def test_a_not_delivered_start_waits_as_long_as_the_gateway_asks(clock):
    coord, notify, _ = build()
    notify.fail_next("start", NotifyUpstreamError(
        "Apple could not process the Live Activity start", 502,
        {"deliveryState": "not-delivered", "retryAfterSeconds": 900},
    ))
    run(coord.handle(status(clock)))
    assert coord._sessions[SERIAL].activity_id is None
    blocked = coord.snapshot()["starts_blocked_for"]
    assert 800 < blocked <= 900, f"used {blocked}s where the gateway asked for 900"


def test_a_dead_credential_is_reported_as_an_error_but_retried_promptly(clock, caplog):
    """The gateway's message says to open the app. A retry against a dead
    credential is answered locally with a 409 and costs nothing, so a long
    hold would only delay tiles after the user had done as asked."""
    from notify_bambuddy.coordinator import SERVICE_BACKOFF_SECONDS
    coord, notify, _ = build()
    notify.fail_next("start", NotifyUpstreamError(
        "The device's Live Activity start token is no longer valid", 502,
        {"deliveryState": "not-delivered"},
    ))
    caplog.set_level(logging.WARNING)
    run(coord.handle(status(clock)))
    snap = coord.snapshot()
    assert 0 < snap["starts_blocked_for"] <= SERVICE_BACKOFF_SECONDS
    assert "start token is no longer valid" in snap["starts_blocked_reason"]
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_bodyless_502_from_a_proxy_is_a_passing_outage(clock, caplog):
    """A 502 with no gateway body is something in front of the gateway. It
    must not be treated as the gateway saying retrying is pointless."""
    from notify_bambuddy.coordinator import SERVICE_BACKOFF_SECONDS
    coord, notify, _ = build()
    notify.fail_next("start", NotifyUpstreamError("Bad Gateway", 502, {}))
    caplog.set_level(logging.WARNING)
    run(coord.handle(status(clock)))
    assert 0 < coord.snapshot()["starts_blocked_for"] <= SERVICE_BACKOFF_SECONDS
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_an_adopted_unknown_tile_is_not_rolled_over_by_the_sessions_age(clock):
    """The 502 carries no expiry. Without one the rollover timed the tile by
    the SESSION's age and ended it seconds after it started late in a print."""
    coord, notify, _ = build()
    run(coord.handle(status(clock)))
    session = coord._sessions[SERIAL]
    clock.advance(coord._config.rollover_after_s + 600)
    session.activity_id = None
    session.delivery = Delivery.PENDING
    notify.fail_next("start", NotifyUpstreamError(
        "delivery is unknown", 502, {"deliveryState": "unknown", "activityId": "LALATE01"},
    ))
    run(coord._start_tile(session, clock.time()))
    assert session.activity_id == "LALATE01"
    # Keep the print demonstrably alive, or the silence sweep retires it before
    # the rollover is ever consulted and this test passes for the wrong reason.
    session.last_status_ts = clock.time()
    assert session.phase is Phase.PRINTING
    starts_before = len(notify.started)
    run(coord.tick())
    assert session.activity_id == "LALATE01", "rolled over a tile that had only just started"
    assert len(notify.started) == starts_before


def test_an_edge_rate_limit_is_brief_and_says_nothing_about_the_app(clock):
    coord, notify, _ = build()
    notify.fail_next("start", NotifyRateLimited("Too Many Requests", 429, {}, retry_after=45))
    run(coord.handle(status(clock)))
    snap = coord.snapshot()
    assert 0 < snap["starts_blocked_for"] <= 45
    assert "app" not in snap["starts_blocked_reason"].lower()


# --- dead-token -------------------------------------------------------------


def test_dead_token_is_not_a_swipe(clock):
    """The gateway sets it only when the START credential is refused while the
    row is still waiting for its tile: no tile ever existed. Recording it as a
    dismissal silenced the printer for a print the user never saw a tile for."""
    coord, notify, store = build()
    run(coord.handle(status(clock)))
    first = notify.started[0]["activityId"]
    _gone(notify, first, "dead-token")
    clock.advance(30)
    run(coord.handle(status(clock, progress=20.0)))
    clock.advance(30)
    run(coord.handle(status(clock, progress=25.0)))
    assert not store.is_dismissed(SERIAL), "recorded a swipe nobody made"
    assert len(notify.started) == 2, "the print was left with no tile"
    assert coord._sessions[SERIAL].recreate_attempts == 1, (
        "the replacement must be counted, or nothing bounds it"
    )


def test_a_real_swipe_is_still_honoured(clock):
    coord, notify, store = build()
    run(coord.handle(status(clock)))
    first = notify.started[0]["activityId"]
    _gone(notify, first, "dismissed")
    clock.advance(30)
    run(coord.handle(status(clock, progress=20.0)))
    clock.advance(30)
    run(coord.handle(status(clock, progress=25.0)))
    assert len(notify.started) == 1


# --- segments and the phase word --------------------------------------------


def _session(**overrides):
    values = dict(
        session_key=f"{SERIAL}:job", serial=SERIAL, printer_name="X1C-1",
        phase=Phase.PRINTING, delivery=Delivery.ACTIVE, activity_id="LA1",
        job_name="mascot.3mf", progress=40.0, eta_seconds=None,
        progress_trustworthy=True, created_at=1000.0,
    )
    values.update(overrides)
    return SessionState(**values)


SEGMENTS = dict(tile_progress="segments", tile_segments=10)
RUNOUT = {"full_code": "0C00200000020001", "severity": 2,
          "description": "Filament ran out. Please load new filament."}


def test_a_paused_segmented_tile_says_why_instead_of_a_fraction():
    """The slot runs countdown, trailing, step fraction, status. With segments
    the fraction is always there, so the status word could never render: the
    headline "Filament ran out" became "4/10"."""
    c = build_content(_session(phase=Phase.PAUSED, hms_errors=(RUNOUT,)),
                      make_config(**SEGMENTS), initial=False)
    assert c["trailing"] == "Filament ran out"


def test_a_silent_segmented_tile_says_connection_lost():
    c = build_content(_session(phase=Phase.SILENT), make_config(**SEGMENTS), initial=False)
    assert c["trailing"] == "Connection lost"


def test_a_failed_segmented_tile_ends_on_the_outcome_not_a_fraction():
    s = _session(phase=Phase.TERMINAL, outcome=Outcome.FAILED)
    c = terminal_content(s, make_config(**SEGMENTS))
    assert c["trailing"] == "Failed"


def test_a_smooth_bar_still_leaves_the_word_to_the_status_field():
    """Nothing outranks `status` without segments, so `trailing` stays clear
    rather than repeating it."""
    c = build_content(_session(phase=Phase.PAUSED), make_config(), initial=False)
    assert c["trailing"] is None
    assert c["status"] == "Paused"


def test_a_printing_segmented_tile_keeps_its_fraction():
    """While printing with no countdown, the fraction IS the useful thing."""
    c = build_content(_session(phase=Phase.PRINTING), make_config(**SEGMENTS), initial=False)
    assert not c.get("trailing")


# --- undeliverable alerts ----------------------------------------------------


def test_an_undeliverable_alert_is_logged_as_an_error_with_the_remedy(clock, caplog):
    """On a browser, alerts are the only output. A retired subscription failed
    every one, and the log said only "Failed to send notification"."""
    coord, notify, _ = build(with_alerts=True)
    notify.fail_next("notify", NotifyDeliveryFailed(
        "Failed to send notification", 400,
        {"apnsStatus": "DEAD_SUBSCRIPTION",
         "apnsError": "Web push subscription retired or missing"},
    ))
    caplog.set_level(logging.WARNING)
    run(coord._alerts.print_finished(SERIAL, "X1C-1", "mascot", Outcome.FAILED))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a retired subscription was logged below error"
    assert "Notify web app" in errors[0].getMessage()


def test_a_passing_delivery_failure_is_not_an_error(clock, caplog):
    """An Apple blip is not something to page anyone about."""
    coord, notify, _ = build(with_alerts=True)
    notify.fail_next("notify", NotifyDeliveryFailed(
        "Failed to send notification", 400, {"apnsStatus": 503, "apnsError": "ServiceUnavailable"},
    ))
    caplog.set_level(logging.WARNING)
    run(coord._alerts.print_finished(SERIAL, "X1C-1", "mascot", Outcome.FAILED))
    assert caplog.records, "the failure vanished"
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_retired_browser_registration_points_at_the_web_app():
    exc = NotifyDeliveryFailed("Failed to send notification", 400,
                               {"apnsStatus": 410, "apnsError": {"reason": "Unregistered"}})
    assert "web app" in exc.hint and exc.actionable


def test_the_unconfirmed_warning_does_not_blame_a_closed_app(clock, caplog):
    """It used to say a healthy tile goes unconfirmed because the app confirms
    only while it is open. Notify's architecture says push-to-start launches
    the app in the background, where it reports the token, so a tile that
    appears is normally confirmed within seconds. Telling users "nothing is
    wrong" while their tiles silently failed was the cost."""
    coord, notify, _ = build()
    notify.leave_unconfirmed = True
    run(coord.handle(status(clock)))
    caplog.set_level(logging.WARNING)
    clock.advance(coord._config.start_verify_after_s + 5)
    run(coord.tick())
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "not been confirmed" in said, "the check did not run"
    assert "while it is open" not in said
    assert "nothing is wrong" not in said
