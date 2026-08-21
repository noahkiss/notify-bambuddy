# ---------------------------------------------------------------------------
# The unit conversion and the content mapping.
#
# The first test in this file is the most important test in the project. If
# `remaining_time` is ever read as seconds, every countdown on every tile is
# wrong by a factor of sixty, and the failure is silent: the tile appears, it
# looks plausible, and it finishes an hour early. The BambuBuddy wiki's own
# example payload actively teaches the wrong unit.
# ---------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timezone

from conftest import SERIAL, make_config

from notify_bambuddy.content import (
    MAX_ENDS_IN,
    build_content,
    commit_sent_eta,
    job_display_name,
    layer_text,
    payload_hash,
    status_word,
    terminal_content,
)
from notify_bambuddy.models import (
    Delivery,
    Outcome,
    Phase,
    PrinterStatus,
    SessionState,
)


def make_status(**overrides) -> PrinterStatus:
    values = dict(
        serial=SERIAL,
        printer_id=1,
        printer_name="X1C-1",
        timestamp=datetime.now(timezone.utc),
        connected=True,
        state="RUNNING",
        progress=45.5,
        remaining_minutes=100,
        layer_num=150,
        total_layers=300,
        subtask_name="Benchy",
        gcode_file="/data/Metadata/plate_1.gcode",
        current_print="Benchy",
        awaiting_plate_clear=False,
    )
    values.update(overrides)
    return PrinterStatus(**values)


def make_session(**overrides) -> SessionState:
    values = dict(
        session_key=f"{SERIAL}:1000",
        serial=SERIAL,
        printer_name="X1C-1",
        phase=Phase.PRINTING,
        delivery=Delivery.ACTIVE,
        activity_id="LA000001",
        job_name="Benchy",
        progress=45.5,
        eta_seconds=6000,
        layer_num=150,
        total_layers=300,
        progress_trustworthy=True,
        created_at=1000.0,
    )
    values.update(overrides)
    return SessionState(**values)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_remaining_time_is_minutes_not_seconds():
    """100 minutes remaining must become 6000 seconds, not 100.

    BambuBuddy publishes the printer firmware's `mc_remaining_time` verbatim,
    which is minutes. Its own other consumer multiplies by 60 with the comment
    "Convert minutes to seconds"; the status route does not, so this service
    has to.
    """
    assert make_status(remaining_minutes=100).eta_seconds == 6000


def test_zero_remaining_time_means_unknown_not_finished():
    """`remaining_time: 0` is "no ETA yet", and must never become `endsIn: 0`.

    It reads 0 during pre-print calibration and at print start, because
    BambuBuddy defaults the field to 0 rather than null. Sending `endsIn: 0`
    is a 400, and a 400 on the START call means no tile at all for that print.
    """
    assert make_status(remaining_minutes=0).eta_seconds is None
    assert make_status(remaining_minutes=-5).eta_seconds is None


def test_ends_in_is_omitted_when_the_eta_is_unknown():
    session = make_session(eta_seconds=None)
    content = build_content(session, make_config(), initial=True)
    # Cleared rather than absent: the phase says no countdown belongs here.
    assert content["endsIn"] is None


def test_ends_in_is_clamped_to_the_api_ceiling():
    """A 30-hour plate must not blow the 86400 second cap.

    Over the cap the gateway answers with a message about milliseconds, which
    is exactly the wrong debugging hint when the units were already right.
    """
    session = make_session(eta_seconds=30 * 3600)  # 108000
    content = build_content(session, make_config(), initial=True)
    assert content["endsIn"] == MAX_ENDS_IN


# ---------------------------------------------------------------------------
# The trailing slot holds exactly one thing
# ---------------------------------------------------------------------------


def test_countdown_and_layer_counter_are_never_sent_together():
    """The widget renders countdown > trailing > status, so only one shows.

    With a countdown on screen the layer counter is explicitly CLEARED, not
    merely omitted: an omitted field keeps its old value, and a stale
    "Layer 12/300" left in `trailing` would hide the status word the moment
    the countdown lapsed.
    """
    content = build_content(make_session(), make_config(), initial=False)
    assert content["endsIn"] == 6000
    assert content["trailing"] is None


def test_the_layer_counter_fills_in_while_the_eta_is_unknown():
    session = make_session(eta_seconds=None, phase=Phase.PRINTING)
    content = build_content(session, make_config(), initial=False)
    assert content["endsIn"] is None
    assert content["trailing"] == "Layer 150/300"
    assert content["status"] == "Printing"


def test_a_paused_print_shows_the_word_not_the_layer_counter():
    """`trailing` outranks `status` on the widget. Sending the layer counter on
    a paused print would show "Layer 150/300" and hide "Paused", which is the
    one word the tile exists to show in that phase."""
    session = make_session(eta_seconds=None, phase=Phase.PAUSED)
    content = build_content(session, make_config(), initial=False)
    assert content["endsIn"] is None
    assert content["trailing"] is None
    assert content["status"] == "Paused"


def test_a_silent_print_shows_connection_lost_unobstructed():
    session = make_session(phase=Phase.SILENT)
    content = build_content(session, make_config(), initial=False)
    assert content["trailing"] is None
    assert content["endsIn"] is None
    assert content["status"] == "Connection lost"


def test_control_characters_never_reach_the_tile():
    """NUL is rejected by the gateway outright; the rest render as nothing."""
    session = make_session(job_name="Ben\x00chy\x1b[0m \u2028dragon")
    content = build_content(session, make_config(), initial=True)
    assert "\x00" not in content["body"]
    assert "\x1b" not in content["body"]
    assert content["body"] == "Ben chy [0m dragon"


def test_string_limits_are_measured_in_utf16_units_like_the_gateway():
    """JavaScript counts UTF-16 code units. 120 emoji are 240 units, and
    trimming by Python code points would still earn a 400."""
    session = make_session(printer_name="🖨" * 120)
    content = build_content(session, make_config(), initial=True)
    assert len(content["title"].encode("utf-16-le")) // 2 <= 120


def test_status_is_always_sent_so_it_can_take_over_from_a_lapsed_countdown():
    content = build_content(make_session(), make_config(), initial=False)
    assert content["status"] == "Printing"
# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_the_display_name_prefers_the_subtask_name():
    assert job_display_name("Articulated Dragon", "/data/Metadata/plate_3.gcode") == \
        "Articulated Dragon"


def test_a_plate_filename_carries_no_information_and_is_dropped():
    """`plate_2` tells the user strictly less than an empty second line."""
    assert job_display_name(None, "/data/Metadata/plate_2.gcode") is None
    assert job_display_name("", "/data/Metadata/plate_11.gcode") is None


def test_a_real_filename_survives_with_its_extension_stripped():
    assert job_display_name(None, "/cache/Calibration Cube.gcode.3mf") == "Calibration Cube"
    assert job_display_name(None, "benchy.3mf") == "benchy"


def test_privacy_mode_keeps_the_filename_off_the_lock_screen():
    content = build_content(make_session(), make_config(privacy_mode=True), initial=True)
    # Cleared, not omitted: turning privacy mode on and restarting must take
    # the name OFF a tile already showing it, and an absent field is kept.
    assert content["body"] is None
    assert content["title"] == "X1C-1"


def test_an_unchanged_eta_is_not_resent():
    """`endsIn` is relative, so the gateway recomputes the finish from "now"
    every time it arrives. Resending the same value fifteen seconds later
    quietly pushes the finish out, and a tile updated all print long would
    drift later while never appearing to change."""
    session = make_session(eta_seconds=6000, last_status_ts=1000.0)
    first = build_content(session, make_config(), initial=False)
    assert first["endsIn"] == 6000

    # Building alone changes nothing: the payload may still be dropped by the
    # pacing floor, a gateway backoff, or a failed request. Only a payload the
    # gateway ACCEPTED is on the tile.
    assert session.sent_eta_seconds is None
    commit_sent_eta(session, first)
    assert session.sent_eta_seconds == 6000

    # 30 seconds later the printer still says 100 minutes. On screen the
    # countdown has already ticked down to ~5970, which is close enough.
    session.last_status_ts = 1030.0
    again = build_content(session, make_config(), initial=False)
    assert "endsIn" not in again, "an unchanged estimate must not be rewritten"

    # A real change is sent.
    session.last_status_ts = 1060.0
    session.eta_seconds = 3000
    moved = build_content(session, make_config(), initial=False)
    assert moved["endsIn"] == 3000


def test_a_countdown_that_was_never_sent_is_not_remembered_as_sent():
    """The bug this separation exists for. `build_content` is called
    speculatively all over the coordinator, and a payload that is then dropped
    must not leave the session believing its countdown reached the tile: the
    recorded value decays at the same rate as the printer's estimate, so the
    two agree forever and the countdown is never sent again."""
    session = make_session(eta_seconds=7200, last_status_ts=1000.0)
    build_content(session, make_config(), initial=True)      # start payload, POST fails
    assert session.sent_eta_seconds is None, "nothing reached the tile"

    # Two minutes later the start is retried. It must still carry a countdown.
    session.last_status_ts = 1120.0
    session.eta_seconds = 7080
    retry = build_content(session, make_config(), initial=True)
    assert retry["endsIn"] == 7080


# ---------------------------------------------------------------------------
# Numbers that cannot be trusted
# ---------------------------------------------------------------------------


def test_progress_is_pinned_to_zero_until_it_can_be_believed():
    """A new print inherits the previous print's percentage.

    BambuBuddy never resets its progress field at print start, so the first
    frames of a new print carry the last one's value. Without this pin a fresh
    tile appears at 97% and then jumps backwards.
    """
    session = make_session(progress=97.0, progress_trustworthy=False)
    content = build_content(session, make_config(), initial=True)
    assert content["progress"] == 0


def test_progress_is_clamped_and_floored():
    assert build_content(make_session(progress=145.0), make_config(),
                         initial=False)["progress"] == 100
    assert build_content(make_session(progress=-3.0), make_config(),
                         initial=False)["progress"] == 0
    # Floored, not rounded: 99.7% must not read as a finished print.
    assert build_content(make_session(progress=99.7), make_config(),
                         initial=False)["progress"] == 99


def test_the_layer_counter_is_dropped_when_the_numbers_disagree():
    """`total_layers` can still hold the PREVIOUS print's denominator.

    BambuBuddy only applies positive values, because the firmware resets the
    total to 0 at print end and the last good denominator is kept instead. At
    the start of a new print that produces pairs like `3/1247` spanning two
    different prints.
    """
    assert layer_text(make_session(layer_num=412, total_layers=1247)) == "Layer 412/1247"
    assert layer_text(make_session(layer_num=5, total_layers=0)) is None
    assert layer_text(make_session(layer_num=1300, total_layers=1247)) is None
    assert layer_text(make_session(layer_num=0, total_layers=300)) is None


# ---------------------------------------------------------------------------
# Terminal content
# ---------------------------------------------------------------------------


def test_a_successful_print_ends_at_one_hundred_percent():
    session = make_session(phase=Phase.TERMINAL, outcome=Outcome.COMPLETED, progress=99.4)
    content = terminal_content(session, make_config())
    assert content["progress"] == 100
    assert content["status"] == "Done"
    assert content["endsIn"] is None


def test_a_failed_print_keeps_the_progress_it_actually_reached():
    """Rounding a failure up to a full bar erases the useful detail."""
    session = make_session(phase=Phase.TERMINAL, outcome=Outcome.FAILED, progress=40.0)
    content = terminal_content(session, make_config())
    assert content["progress"] == 40
    assert content["status"] == "Failed"


def test_a_touchscreen_stop_is_worded_more_cautiously_than_a_cancel():
    """BambuBuddy can only say "cancelled" for stops it issued itself."""
    assert status_word(make_session(phase=Phase.TERMINAL, outcome=Outcome.CANCELLED)) == \
        "Cancelled"
    assert status_word(make_session(phase=Phase.TERMINAL, outcome=Outcome.ABORTED)) == "Stopped"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_long_strings_are_trimmed_rather_than_rejected_by_the_api():
    session = make_session(printer_name="P" * 200, job_name="J" * 400)
    content = build_content(session, make_config(), initial=True)
    assert len(content["title"]) <= 120
    assert len(content["body"]) <= 300


def test_the_payload_hash_ignores_key_order():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_outcome_ranking_lets_a_late_success_overrule_an_earlier_abort():
    """BambuBuddy's restart reconciliation synthesises "aborted" for prints
    that are still running, and says outright that the real completion
    overwrites it if it arrives later."""
    assert Outcome.COMPLETED.rank > Outcome.ABORTED.rank
    assert Outcome.COMPLETED.rank > Outcome.FAILED.rank
    assert Outcome.FAILED.rank > Outcome.SUPERSEDED.rank
