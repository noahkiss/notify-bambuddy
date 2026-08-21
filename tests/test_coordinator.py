# ---------------------------------------------------------------------------
# The state machine, scenario by scenario.
#
# Every test in this file corresponds to something that actually happens on
# real hardware and that a naive implementation gets wrong. The names are
# written as the behaviour, not as the method under test, because in six
# months the question someone will be asking is "why does it do that?" and the
# test name should answer it.
#
# The coordinator has exactly ONE input: a `PrinterStatus` reading from the
# BambuBuddy REST poller. There are no start events and no terminal events, so
# every scenario below is expressed as a sequence of readings and a clock.
#
# Time is faked. The coordinator reads the clock through its module-level
# `time` name, which the `clock` fixture in conftest.py replaces, so a
# two-minute terminal hold or an eight-hour rollover is tested in microseconds.
# ---------------------------------------------------------------------------

from __future__ import annotations

from conftest import SERIAL, SERIAL_B, FakeNotify, build, make_config, run, status

from notify_bambuddy import coordinator as coordinator_module
from notify_bambuddy.coordinator import Coordinator
from notify_bambuddy.models import Outcome, Phase


def reach_terminal(coord, clock, *, state: str = "FINISH", **frame) -> None:
    """Drive a session to a terminal outcome, the only way there is now.

    Nothing reports the end of a print. It is INFERRED from the printer
    holding an inactive state (FINISH, FAILED or IDLE) for longer than
    `INACTIVE_GRACE_SECONDS`, which takes two readings either side of that
    window. Doing it in one place keeps the ninety-second wait from burying
    the behaviour each test is actually about.
    """
    run(coord.handle(status(clock, state=state, **frame)))
    clock.advance(coordinator_module.INACTIVE_GRACE_SECONDS + 1)
    run(coord.handle(status(clock, state=state, **frame)))


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_print_start_creates_one_tile(clock):
    """The three fields a user actually reads, in the payload that creates the
    tile. Title is the machine, because the tile's job is to answer "which of
    mine is this?"; body is the model; the symbol is what makes it recognisable
    on a crowded Lock Screen."""
    coord, notify, _ = build()
    run(coord.handle(status(clock, printer_name="X1C-1", subtask_name="Benchy")))
    assert len(notify.started) == 1
    content = notify.started[0]["content"]
    assert content["title"] == "X1C-1"
    assert content["body"] == "Benchy"
    assert content["symbol"] == "printer.fill"


def test_the_countdown_uses_minutes_times_sixty(clock):
    """End-to-end proof of the unit conversion, through the state machine."""
    coord, notify, _ = build()
    run(coord.handle(status(clock, remaining_minutes=100)))
    assert notify.started[0]["content"]["endsIn"] == 6000


def test_two_printers_get_two_separate_tiles(clock):
    coord, notify, _ = build()
    run(coord.handle(status(clock, serial=SERIAL)))
    run(coord.handle(status(clock, serial=SERIAL_B, printer_name="A1-2")))
    assert len(notify.started) == 2
    assert len({entry["activityId"] for entry in notify.started}) == 2


def test_progress_updates_are_paced(clock):
    coord, notify, _ = build()
    run(coord.handle(status(clock, progress=10.0, layer_num=10)))
    notify.updates.clear()

    # A second frame moments later must not produce a push.
    clock.advance(2)
    run(coord.handle(status(clock, progress=11.0, layer_num=11)))
    assert notify.updates == []

    # Past the floor, it does.
    clock.advance(20)
    run(coord.handle(status(clock, progress=13.0, layer_num=13)))
    assert len(notify.updates) == 1
    assert notify.updates[0][1]["progress"] == 13


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def test_a_print_already_under_way_gets_a_tile(clock):
    """Polling never sees a print BEGIN, and it does not need to.

    Every tile this service creates comes from noticing a printer that is
    already running, whether the print started five seconds before the poll or
    was half done when the service booted. A reading of RUNNING with no open
    session, sustained for a grace window, is the one and only creation
    trigger.
    """
    coord, notify, _ = build()
    run(coord.handle(status(clock, state="RUNNING", progress=40.0)))
    assert len(notify.started) == 1


def test_creation_waits_out_the_grace_window(clock):
    coord, notify, _ = build(make_config(running_grace_s=30))
    run(coord.handle(status(clock)))
    assert notify.started == []

    clock.advance(31)
    run(coord.handle(status(clock)))
    assert len(notify.started) == 1


def test_a_stale_reading_never_creates_a_tile(clock):
    """The single most damaging omission a design here can have.

    A reading is evidence about the moment it was taken, not about now. The
    poller stamps arrival time and then queues the reading, so one that waited
    out a gateway outage can be minutes old before the coordinator sees it,
    and a system clock that steps makes it look older still. Without a
    freshness gate, an old reading creates a tile for a print that is over and
    then keepalives it for eight hours.
    """
    coord, notify, _ = build(make_config(retained_max_age_s=180))
    run(coord.handle(status(clock, offset=-4000)))  # over an hour old
    assert notify.started == []


def test_an_unknown_state_does_nothing_at_all(clock):
    """`"unknown"` is BambuBuddy's placeholder, written before the first
    telemetry frame answers after a reconnect. It is not a transition."""
    coord, notify, _ = build()
    run(coord.handle(status(clock, state="unknown")))
    assert notify.started == []

    run(coord.handle(status(clock, state="RUNNING")))
    assert len(notify.started) == 1
    notify.updates.clear()

    # And it must never end a live tile either.
    run(coord.handle(status(clock, state="unknown")))
    assert notify.ended == []


def test_a_disconnected_payload_does_not_advance_the_tile(clock):
    """`connected: false` means every other field is a last-seen value."""
    coord, notify, _ = build()
    run(coord.handle(status(clock, progress=50.0)))
    notify.updates.clear()
    clock.advance(60)
    run(coord.handle(status(clock, connected=False, progress=99.0)))
    assert notify.updates == []


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------


def test_a_pause_shows_the_phase_and_drops_the_countdown(clock):
    coord, notify, _ = build()
    run(coord.handle(status(clock)))
    notify.updates.clear()

    run(coord.handle(status(clock, state="PAUSE")))
    assert notify.updates, "a pause must reach the tile immediately"
    _, content = notify.updates[-1]
    assert content["status"] == "Paused"
    assert content["endsIn"] is None


def test_a_pause_seconds_after_the_start_is_not_rendered(clock):
    """BambuBuddy's own plate detection pauses a print seconds after it starts
    when it sees objects on the bed. Rendering that would make every print
    flicker through "Paused" on its way to "Printing"."""
    coord, notify, _ = build(make_config(pause_suppress_s=15))
    run(coord.handle(status(clock)))
    notify.updates.clear()
    clock.advance(3)
    run(coord.handle(status(clock, state="PAUSE")))
    assert notify.updates == []


# ---------------------------------------------------------------------------
# How a print ends
#
# There is no terminal event to wait for any more. Every ending in this
# section is inferred, either from an inactive state that persists or from a
# printer that stops answering altogether.
# ---------------------------------------------------------------------------


def test_an_inactive_printer_closes_the_tile_only_once_the_state_persists(clock):
    """The whole terminal path, in one test.

    A single inactive reading proves nothing: a printer drops through IDLE
    between plates and can answer one poll mid-transition. So the tile closes
    only when the printer has held an inactive state for longer than the
    inactive grace, which is the evidence that the print really is over.
    """
    coord, notify, _ = build(make_config(terminal_hold_s=0))
    run(coord.handle(status(clock)))

    run(coord.handle(status(clock, state="FINISH")))
    assert notify.ended == [], "one inactive reading is not an ending"

    clock.advance(coordinator_module.INACTIVE_GRACE_SECONDS + 1)
    run(coord.handle(status(clock, state="FINISH")))
    run(coord.tick())
    assert len(notify.ended) == 1


def test_the_outcome_follows_the_state_the_printer_settled_in(clock):
    """FINISH, FAILED and IDLE are three different pieces of news and the tile
    says which. Collapsing them into one "ended" would tell a user whose print
    failed at 3am that it finished."""
    for state, word in (("FINISH", "Done"), ("FAILED", "Failed"), ("IDLE", "Stopped")):
        coord, notify, _ = build(make_config(terminal_hold_s=120))
        run(coord.handle(status(clock)))
        reach_terminal(coord, clock, state=state)
        assert notify.updates[-1][1]["status"] == word, state


def test_a_completion_is_held_before_the_tile_is_ended(clock):
    """Ending is irreversible, and an outcome read off a printer state is an
    inference that later readings can still improve on. The terminal content
    goes up at once; the tile itself is held."""
    coord, notify, _ = build(make_config(terminal_hold_s=120))
    run(coord.handle(status(clock)))
    reach_terminal(coord, clock)

    # Terminal content is on screen immediately...
    _, content = notify.updates[-1]
    assert content["status"] == "Done"
    assert content["progress"] == 100
    # ...but the tile is still there, and STAYS there across ticks inside the
    # hold. Ticking is the part that matters: without it, removing the hold
    # entirely would pass this test.
    assert notify.ended == []
    clock.advance(60)
    run(coord.tick())
    assert notify.ended == [], "the tile must survive a tick inside the hold window"

    clock.advance(61)
    run(coord.tick())
    assert len(notify.ended) == 1
    assert notify.ended[0][2] == 300  # keepFor


def test_a_later_failure_cannot_overwrite_a_success(clock):
    """Once the tile says Done, nothing may take it back.

    A printer that has reported FINISH long enough to be believed goes on
    reporting: it drops to IDLE when the plate is cleared, and can report
    FAILED for the NEXT job's aborted start. Letting any of that reach a
    finished session would put "Print stopped" on a print that succeeded.
    """
    coord, notify, _ = build(make_config(terminal_hold_s=120))
    run(coord.handle(status(clock)))
    reach_terminal(coord, clock)
    assert notify.updates[-1][1]["status"] == "Done"
    session = coord._sessions[SERIAL]
    before = len(notify.updates)

    clock.advance(30)
    reach_terminal(coord, clock, state="FAILED")
    assert len(notify.updates) == before, "nothing may repaint a finished tile"
    assert session.outcome is Outcome.COMPLETED


def test_a_terminal_is_matched_on_serial_not_on_the_job_name(clock):
    """A multi-plate job renames itself part way through, so the name attached
    to the reading that ends a print is often not the one it started under.
    Correlating on the name leaves the tile stuck at 99% forever; the open
    session for a serial IS the print that just ended."""
    coord, notify, _ = build(make_config(terminal_hold_s=0))
    run(coord.handle(status(clock, subtask_name="Plate one")))
    reach_terminal(coord, clock, subtask_name="Plate four")
    run(coord.tick())
    assert len(notify.ended) == 1


def test_a_status_frame_cannot_resurrect_a_finished_print(clock):
    """A printer does not go quiet the moment a print ends, and BambuBuddy's
    own view of it can lag: a stale RUNNING can answer a poll after FINISH has
    already been believed."""
    coord, notify, _ = build(make_config(terminal_hold_s=120))
    run(coord.handle(status(clock)))
    reach_terminal(coord, clock)
    count = len(notify.updates)
    run(coord.handle(status(clock, state="RUNNING", progress=88.0)))
    assert len(notify.updates) == count

    # The consequence that actually matters: a resurrected session would have
    # its phase dragged back to PRINTING, and the hold would then never expire,
    # leaving the tile on the Lock Screen forever.
    session = coord._sessions[SERIAL]
    assert session.phase is Phase.TERMINAL
    assert session.outcome is Outcome.COMPLETED
    clock.advance(121)
    run(coord.tick())
    assert len(notify.ended) == 1, "the tile still ends on schedule"


def test_a_momentary_inactive_frame_does_not_end_a_print_that_is_still_running(clock):
    """A printer that blips through an inactive state for one poll and goes
    straight back to RUNNING has not stopped.

    BambuBuddy answers from its own cached view of the machine, which goes
    briefly stale whenever its link to the printer reconnects. Ending on a
    single reading is what would wipe a tile with four hours left, so the
    inactive watch is reset the moment RUNNING comes back.
    """
    coord, notify, _ = build(make_config(terminal_hold_s=120, progress_min_interval_s=0))
    run(coord.handle(status(clock, progress=40.0, layer_num=120)))

    # Blips, forever, at a cadence far longer than the inactive grace. The
    # tile must survive every one of them.
    for _ in range(5):
        clock.advance(60)
        run(coord.handle(status(clock, state="IDLE", progress=40.0, layer_num=120)))
        clock.advance(60)
        run(coord.handle(status(clock, state="RUNNING", progress=45.0, layer_num=135)))
        run(coord.tick())
    assert notify.ended == [], "one inactive reading must never end a live print"
    assert coord._sessions[SERIAL].phase is Phase.PRINTING

    # And the print finishes properly.
    reach_terminal(coord, clock)
    clock.advance(121)
    run(coord.tick())
    assert len(notify.ended) == 1
    assert notify.ended[0][1]["status"] == "Done"


def test_a_status_frame_older_than_the_last_one_applied_is_dropped(clock):
    """A reading stamped before one already applied must never be applied: it
    would rewind the tile. Readings are stamped on arrival, so this is what a
    clock stepping backwards looks like."""
    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock, progress=50.0, layer_num=150)))
    clock.advance(30)
    run(coord.handle(status(clock, progress=52.0, layer_num=156)))
    assert coord._sessions[SERIAL].progress == 52.0
    count = len(notify.updates)

    run(coord.handle(status(clock, progress=51.0, layer_num=153, offset=-60)))
    assert coord._sessions[SERIAL].progress == 52.0, "the stale frame must not be applied"
    assert len(notify.updates) == count


def test_an_unknown_state_frame_does_not_even_touch_the_session(clock):
    """Pins the no-op, not just the absence of a push. BambuBuddy writes
    "unknown" when it reconnects to a printer, before real telemetry answers,
    and the values alongside it are meaningless."""
    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock, progress=40.0, layer_num=120)))
    clock.advance(30)

    run(coord.handle(status(clock, state="unknown", progress=99.0, layer_num=311)))
    session = coord._sessions[SERIAL]
    assert session.progress == 40.0, "an unknown frame must not write its numbers"
    assert session.layer_num == 120
    assert session.phase is Phase.PRINTING


# ---------------------------------------------------------------------------
# New prints and file changes
# ---------------------------------------------------------------------------


def test_a_file_change_mid_print_updates_the_tile_instead_of_replacing_it(clock):
    """A multi-plate project changes its gcode file at every plate boundary,
    and the printer reports RUNNING straight through while the job name and
    the layer counter both change under it. Ending the tile there would drop
    the user's progress bar mid-job and burn a tile slot during the overlap."""
    coord, notify, _ = build(make_config(progress_min_interval_s=0))
    run(coord.handle(status(clock, subtask_name="Plate one")))
    assert len(notify.started) == 1

    clock.advance(5)
    run(coord.handle(status(clock, subtask_name="Plate two", layer_num=0)))
    assert len(notify.started) == 1, "a file change must not start a second tile"
    assert notify.updates[-1][1]["body"] == "Plate two"

    # And the layer counter climbing again from the reset must not undo it.
    clock.advance(2)
    run(coord.handle(status(clock, subtask_name="Plate two", layer_num=3)))
    assert len(notify.started) == 1
    assert notify.ended == []


def test_a_layer_reset_after_a_gap_is_treated_as_a_new_print(clock):
    """The status-only new-print edge, with the one qualifier that makes it
    safe: the printer was NOT heard from continuously. A genuinely new print
    passes through FINISH or IDLE on the way, so a layer reset after a gap
    longer than the silence threshold is a different job."""
    coord, notify, _ = build(make_config(silence_after_s=90))
    run(coord.handle(status(clock, layer_num=180)))
    clock.advance(600)  # a long gap: the printer went quiet
    run(coord.handle(status(clock, layer_num=2)))
    assert len(notify.started) == 2
    assert len(notify.ended) == 1  # the old tile was superseded


def test_a_layer_reset_while_continuously_running_is_a_plate_change(clock):
    """BambuBuddy resets `layer_num` to 0 on a mid-job FILE CHANGE too, which
    is what a multi-plate project does at every plate boundary, with the
    printer reporting RUNNING straight through. Superseding there would drop
    the user's progress bar and burn a slot at every plate."""
    coord, notify, _ = build(make_config(silence_after_s=90))
    run(coord.handle(status(clock, layer_num=180)))
    clock.advance(5)
    run(coord.handle(status(clock, layer_num=0)))
    clock.advance(5)
    run(coord.handle(status(clock, layer_num=3)))
    assert len(notify.started) == 1, "the same tile carries on across the plate boundary"
    assert notify.ended == []


def test_a_new_print_clears_a_previous_dismissal(clock):
    """Swiping one print's tile away must not silence the next print."""
    coord, notify, store = build(make_config(terminal_hold_s=0))
    run(coord.handle(status(clock)))
    activity_id = notify.started[0]["activityId"]
    notify.dismiss(activity_id)

    clock.advance(30)
    run(coord.handle(status(clock, progress=20.0, layer_num=20)))
    assert len(notify.started) == 1, "the dismissed tile must not come back"

    # The dismissed print ENDS. That is what makes the next one a new print.
    reach_terminal(coord, clock)
    run(coord.tick())

    clock.advance(30)
    run(coord.handle(status(clock, progress=1.0, layer_num=1, subtask_name="Next job")))
    assert len(notify.started) == 2, "a genuinely new print gets a tile"


def test_a_dismissed_tile_stays_dismissed_across_a_plate_change(clock):
    """A multi-plate job resets its layer counter at every plate boundary with
    the printer RUNNING the whole time. A user who swiped the tile away at
    plate one must not get it back at plate two."""
    coord, notify, _ = build(make_config(silence_after_s=90))
    run(coord.handle(status(clock, subtask_name="Big job")))
    notify.dismiss(notify.started[0]["activityId"])
    clock.advance(20)
    run(coord.handle(status(clock, progress=20.0, layer_num=20, subtask_name="Big job")))
    assert len(notify.started) == 1

    clock.advance(5)
    run(coord.handle(status(clock, progress=21.0, layer_num=0, subtask_name="Big job")))
    assert len(notify.started) == 1, "the plate change must not resurrect a dismissed tile"


# ---------------------------------------------------------------------------
# What the gateway does to us
# ---------------------------------------------------------------------------


def test_a_dismissed_tile_is_never_recreated(clock):
    coord, notify, store = build()
    run(coord.handle(status(clock)))
    notify.dismiss(notify.started[0]["activityId"])

    clock.advance(30)
    run(coord.handle(status(clock, progress=30.0, layer_num=30)))
    assert len(notify.started) == 1


def test_a_tile_reaped_by_the_gateway_is_recreated(clock):
    """`overdue`, `abandoned`, `expired` and `never-started` all arrive as 410,
    exactly like a dismissal. Treating them alike is how an integration
    permanently blacklists a working printer the first time an ETA overruns."""
    coord, notify, _ = build()
    run(coord.handle(status(clock)))
    notify.reap(notify.started[0]["activityId"], reason="overdue")

    clock.advance(30)
    run(coord.handle(status(clock, progress=30.0, layer_num=30)))
    assert len(notify.started) == 2


def test_the_five_tile_cap_queues_a_printer_rather_than_writing_it_off(clock):
    """The gateway reports its cap as a 400, which every sane client treats as
    permanent. It is the one transient 400 there is."""
    config = make_config(max_tiles=2)
    coord, notify, _ = build(config, FakeNotify(max_tiles=2))

    for index, serial in enumerate(["S1", "S2", "S3"]):
        run(coord.handle(status(clock, serial=serial, printer_name=f"P{index}",
                                remaining_minutes=100 + index)))

    assert len(notify.started) == 2
    assert coord.snapshot()["waiting_for_slot"] == ["S3"]

    # A slot frees up.
    reach_terminal(coord, clock, serial="S1", printer_name="P0")
    clock.advance(coord._config.terminal_hold_s + 1)
    run(coord.tick())

    assert len(notify.started) == 3, "the queued printer gets the freed slot"


def test_a_tile_that_never_appears_backs_off_further_starts_and_says_why(clock):
    """A start can be accepted by the gateway and by Apple and still never
    produce a tile, because iOS silently throttles push-to-start once the
    device's start credential goes stale. Nothing anywhere reports it.

    The tile is deliberately LEFT ALONE: ending and retrying would be one
    discarded push every ninety seconds forever, and the gateway re-sends a
    still-pending start itself the moment the phone reports a fresh
    credential. What happens instead is an actionable log line and a block
    on further starts.
    """
    coord, notify, _ = build(make_config(start_verify_after_s=90))
    run(coord.handle(status(clock)))
    activity_id = notify.started[0]["activityId"]
    notify.never_started(activity_id)

    clock.advance(91)
    run(coord.tick())

    assert notify.ended == [], "nothing is waiting, so the pending start is left for the gateway"
    snapshot = coord.snapshot()
    assert snapshot["starts_blocked_for"] > 0
    assert "Notify app" in snapshot["starts_blocked_reason"]
    assert snapshot["unconfirmed_starts"] == 1


def test_a_tile_that_never_appears_is_ended_only_when_another_print_is_waiting(clock):
    coord, notify, _ = build(make_config(start_verify_after_s=90, max_tiles=1),
                             FakeNotify(max_tiles=1))
    run(coord.handle(status(clock)))
    ghost = notify.started[0]["activityId"]
    notify.never_started(ghost)
    run(coord.handle(status(clock, serial="WAITING", printer_name="Waiting")))
    assert coord.snapshot()["waiting_for_slot"] == ["WAITING"]

    clock.advance(91)
    run(coord.tick())
    assert ghost in [entry[0] for entry in notify.ended], "the ghost gives up its slot"


def test_a_throttled_device_stops_starts_without_touching_updates(clock):
    from notify_bambuddy.notify_client import NotifyThrottled

    coord, notify, _ = build()
    notify.fail_next("start", NotifyThrottled(
        "Apple is ignoring starts", 429, {"retryAfterSeconds": 1800}
    ))
    run(coord.handle(status(clock)))
    assert notify.started == []
    snapshot = coord.snapshot()
    assert snapshot["starts_blocked_for"] > 0
    assert "Notify app" in snapshot["starts_blocked_reason"]


# ---------------------------------------------------------------------------
# Time-driven behaviour
# ---------------------------------------------------------------------------


def test_a_silent_printer_is_labelled_rather_than_left_frozen(clock):
    """The `connected: false` this would otherwise wait for frequently never
    arrives: BambuBuddy's staleness detector only runs when something asks for
    a status, and after a smart-plug Auto Off the printer stops reporting
    entirely while BambuBuddy goes on answering with its last known values."""
    coord, notify, _ = build(make_config(silence_after_s=90), with_alerts=True)
    run(coord.handle(status(clock, progress=96.0)))
    notify.updates.clear()

    clock.advance(120)
    run(coord.tick())

    _, content = notify.updates[-1]
    assert content["status"] == "Connection lost"
    assert any("not responding" in n["title"].lower() for n in notify.notifications)


def test_the_keepalive_fires_even_when_nothing_changed(clock):
    """It exists to refresh the gateway's freshness clock, so making it
    conditional on a content change would defeat its only purpose. The gateway
    ends a countdown-less tile after two hours of silence, and every paused or
    silent print is exactly that shape."""
    # Silence detection is disabled for this test, deliberately. With the
    # default 90s threshold it would fire first and send the update itself,
    # which would leave this test green even if the keepalive were deleted.
    coord, notify, _ = build(make_config(
        keepalive_interval_s=1200, silence_after_s=9000, abandon_after_s=90000,
    ))
    # Two readings, so the percentage has demonstrably MOVED and is therefore
    # on the tile rather than pinned to zero. That is what makes the number
    # below evidence of anything.
    run(coord.handle(status(clock, progress=5.0)))
    clock.advance(20)
    run(coord.handle(status(clock, progress=10.0)))
    notify.updates.clear()

    clock.advance(600)
    run(coord.tick())
    assert notify.updates == [], "nothing is due yet"

    clock.advance(700)
    run(coord.tick())
    assert len(notify.updates) == 1, "the keepalive fired with identical content"
    # Proof it bypassed the redundant-payload check: the payload it sent is
    # byte-identical to the one already on screen.
    assert notify.updates[-1][1]["progress"] == 10


def test_a_long_print_rolls_over_before_apples_eight_hour_ceiling(clock):
    """Apple ends every tile at eight hours regardless of the countdown, and
    long prints routinely outlast that. The old tile is ended BEFORE the
    successor starts, because the gateway's cap counts starting tiles too."""
    coord, notify, _ = build(make_config(rollover_after_s=27000))
    run(coord.handle(status(clock)))
    first = notify.started[0]["activityId"]

    clock.advance(27001)
    # A real 7.5 hour print is still reporting telemetry the whole time. Without
    # this frame the printer would have been silent for 7.5 hours, and the
    # abandon branch would (correctly) end the tile before the rollover ran.
    run(coord.handle(status(clock, progress=90.0, layer_num=280)))
    run(coord.tick())

    assert first in [entry[0] for entry in notify.ended]
    assert len(notify.started) == 2
    assert notify.started[1]["activityId"] != first


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_an_out_of_order_status_frame_is_dropped_not_applied(clock):
    coord, notify, _ = build()
    run(coord.handle(status(clock, progress=50.0, layer_num=150)))
    notify.updates.clear()

    clock.advance(30)
    run(coord.handle(status(clock, progress=52.0, layer_num=156)))
    count = len(notify.updates)

    # A frame stamped earlier arriving later must not rewind the tile.
    run(coord.handle(status(clock, progress=51.0, layer_num=153, offset=-60)))
    assert len(notify.updates) == count


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def test_a_restart_reattaches_to_a_tile_that_is_still_live(clock):
    coord, notify, store = build()
    run(coord.handle(status(clock)))
    activity_id = notify.started[0]["activityId"]

    # A fresh coordinator over the same store, as after a process restart.
    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())

    assert reborn._sessions[SERIAL].activity_id == activity_id
    assert len(notify.started) == 1, "recovery must not create a second tile"


def test_a_restart_honours_a_dismissal_that_happened_while_we_were_down(clock):
    coord, notify, store = build()
    run(coord.handle(status(clock)))
    notify.dismiss(notify.started[0]["activityId"])

    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())

    clock.advance(30)
    run(reborn.handle(status(clock, progress=40.0, layer_num=40)))
    assert len(notify.started) == 1


def test_a_restart_recreates_a_tile_the_gateway_reaped_while_we_were_down(clock):
    coord, notify, store = build()
    run(coord.handle(status(clock)))
    notify.reap(notify.started[0]["activityId"], reason="expired")

    reborn = Coordinator(coord._config, store, notify, alerts=None)
    run(reborn.recover())

    clock.advance(30)
    run(reborn.handle(status(clock, progress=40.0, layer_num=40)))
    assert len(notify.started) == 2


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_a_finished_print_also_sends_a_real_notification(clock):
    """Live Activity traffic is deliberately kept out of Notify's history, so
    without this a finished print leaves no trace once its tile disappears."""
    coord, notify, _ = build(with_alerts=True)
    run(coord.handle(status(clock)))
    reach_terminal(coord, clock)
    assert len(notify.notifications) == 1
    alert = notify.notifications[0]
    assert "Benchy" in alert["text"]
    assert alert["groupType"] == f"bambuddy-{SERIAL}"
    assert alert["timeSensitive"] is False, "good news must not break through Focus"


def test_a_momentary_inactive_frame_never_sends_the_wrong_alert(clock):
    """A printer that flickers through an inactive state and then finishes
    properly must produce exactly one notification, and it must say finished.
    The alternative leaves a "Print stopped" in the user's history for a print
    that succeeded, which is worse than no notification at all."""
    coord, notify, _ = build(make_config(terminal_hold_s=120), with_alerts=True)
    run(coord.handle(status(clock)))

    clock.advance(30)
    run(coord.handle(status(clock, state="IDLE")))
    clock.advance(30)
    run(coord.handle(status(clock, state="RUNNING", progress=50.0, layer_num=150)))

    reach_terminal(coord, clock)
    clock.advance(121)
    run(coord.tick())
    assert [n["title"] for n in notify.notifications] == ["Print finished"]


def test_an_inferred_stop_does_not_pretend_to_know(clock):
    """A printer given up on after silence gets the "not responding" alert,
    not a "Print stopped" one: the latter would be a guess."""
    coord, notify, _ = build(make_config(silence_after_s=90, abandon_after_s=600,
                                         terminal_hold_s=0), with_alerts=True)
    run(coord.handle(status(clock)))
    clock.advance(120)
    run(coord.tick())            # silence noticed: "not responding"
    clock.advance(600)
    run(coord.tick())            # given up: outcome inferred, no second alert
    run(coord.tick())            # hold (zero) expires, tile ended
    titles = [n["title"] for n in notify.notifications]
    assert titles == ["Printer not responding"]
    assert len(notify.ended) == 1


def test_the_problems_only_level_stays_quiet_about_successes(clock):
    coord, notify, _ = build(make_config(alerts="problems"), with_alerts=True)
    run(coord.handle(status(clock)))
    reach_terminal(coord, clock)
    assert notify.notifications == []


def test_a_new_tile_does_not_flash_starting_before_printing(clock):
    """A tile is created already reading "Printing".

    The phase is classified from what the printer reports at creation rather
    than left unset, so a print does not cost two pushes and a visible
    correction one second apart for no information gain.
    """
    coord, notify, _ = build()
    run(coord.handle(status(clock, state="RUNNING")))
    assert notify.started[0]["content"]["status"] == "Printing"
    assert notify.updates == [], "creation alone must not also produce an update"


def test_a_print_that_is_already_paused_gets_a_paused_tile(clock):
    """Restarting while a print is paused must not show it as printing."""
    coord, notify, _ = build()
    run(coord.handle(status(clock, state="PAUSE")))
    content = notify.started[0]["content"]
    assert content["status"] == "Paused"
    assert content["endsIn"] is None


def test_a_countdown_appearing_bypasses_the_progress_pacing_floor(clock):
    """The first frames of a print often carry no ETA, so the tile starts
    without a countdown. The moment the printer works one out is exactly the
    update that must not wait behind a rate limit built for percentages."""
    coord, notify, _ = build(make_config(progress_min_interval_s=15))
    run(coord.handle(status(clock, remaining_minutes=0)))
    assert notify.started[0]["content"]["endsIn"] is None
    notify.updates.clear()

    clock.advance(3)
    run(coord.handle(status(clock, remaining_minutes=90, layer_num=12)))
    assert notify.updates, "the countdown appearing is news, not a progress tick"
    assert notify.updates[-1][1]["endsIn"] == 5400
