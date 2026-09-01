"""The optional tile layouts.

Notify has no "type" field: the fields present ARE the layout, and they
compose. So these tests are mostly about which fields are absent, present, or
explicitly null, because that is the whole mechanism.
"""

from notify_bambuddy.content import build_content, terminal_content, _segment_for
from notify_bambuddy.models import Delivery, Outcome, Phase, SessionState

from conftest import SERIAL, make_config


def _printing(**overrides):
    values = dict(
        session_key=f"{SERIAL}:job",
        serial=SERIAL,
        printer_name="X1C-1",
        phase=Phase.PRINTING,
        delivery=Delivery.ACTIVE,
        activity_id="LA000001",
        job_name="Benchy",
        progress=45.5,
        eta_seconds=6000,
        progress_trustworthy=True,
        created_at=1000.0,
    )
    values.update(overrides)
    return SessionState(**values)


# --- the default is unchanged ----------------------------------------------


def test_the_default_is_a_smooth_bar_and_nothing_new():
    """Everything added here is opt-in. Someone upgrading sends what they
    always sent, plus explicit nulls that clear layouts they are not using."""
    c = build_content(_printing(progress=42.0), make_config(), initial=True)
    assert c["progress"] == 42
    assert c["steps"] is None, "a segmented bar must not be drawn as well"
    assert c["metrics"] is None
    assert c["button"] is None


# --- segments ---------------------------------------------------------------


def test_segments_replace_the_bar_rather_than_joining_it():
    cfg = make_config(tile_progress="segments", tile_segments=10)
    c = build_content(_printing(progress=42.0), cfg, initial=True)
    assert c["steps"] == 10
    assert c["step"] == 4
    assert c["progress"] is None, (
        "sending progress alongside steps draws two bars, and omitting it "
        "would leave a stale one on a tile that had it before"
    )


def test_a_segment_lights_only_when_its_share_is_finished():
    """Rounding to nearest would light the final block at 95% and leave the
    tile claiming a finished print for the last minutes of a long one."""
    assert _segment_for(0, 10) == 0
    assert _segment_for(9, 10) == 0
    assert _segment_for(10, 10) == 1
    assert _segment_for(99, 10) == 9
    assert _segment_for(100, 10) == 10


def test_a_finished_print_fills_every_segment():
    cfg = make_config(tile_progress="segments", tile_segments=6)
    s = _printing(progress=93.0)
    s.outcome = Outcome.COMPLETED
    c = terminal_content(s, cfg)
    assert c["step"] == 6
    assert c.get("progress") is None, "a full smooth bar must not appear beside the segments"


def test_a_failed_print_keeps_the_segments_it_reached():
    cfg = make_config(tile_progress="segments", tile_segments=10)
    s = _printing(progress=40.0)
    s.outcome = Outcome.FAILED
    c = terminal_content(s, cfg)
    assert c["step"] == 4, "the bar is a record of what happened"


# --- no bar at all ----------------------------------------------------------


def test_off_leaves_a_text_only_tile():
    """Still useful: the countdown and status word carry it."""
    c = build_content(_printing(progress=42.0), make_config(tile_progress="off"), initial=True)
    assert c["progress"] is None
    assert c["steps"] is None
    assert c["status"], "the phase word is what makes a bar-less tile readable"


# --- metrics ----------------------------------------------------------------


def test_metrics_are_off_unless_asked_for_because_they_replace_the_body():
    c = build_content(_printing(job_name="mascot"), make_config(), initial=True)
    assert c["metrics"] is None
    assert c["body"] == "mascot"


def test_metrics_carry_progress_with_the_sign_in_the_value():
    """A percentage VALUE earns a mini pill-bar from the widget, which a
    value of "61" with unit "%" does not."""
    s = _printing(progress=61.0, eta_seconds=2820)
    c = build_content(s, make_config(tile_metric_fields=("progress", "eta")), initial=True)
    m = c["metrics"]
    assert m[0] == {"label": "Progress", "value": "61%"}
    assert {"label": "Time left", "value": "47", "unit": "min"} in m
    assert len(m) == 2, (
        "two chips fill the row on a phone. Notify allows six, but a third "
        "here would repeat the status word, which the trailing slot already shows"
    )
    assert not any(x["label"] == "Status" for x in m)


def test_metrics_drop_the_countdown_when_there_is_nothing_to_count():
    s = _printing(progress=61.0, eta_seconds=None)
    c = build_content(s, make_config(tile_metric_fields=("progress", "eta")), initial=True)
    assert not any(x["label"] == "Time left" for x in c["metrics"])


# --- button -----------------------------------------------------------------


def test_no_button_unless_a_url_is_configured():
    assert build_content(_printing(), make_config(), initial=True)["button"] is None


def test_the_button_is_sent_once_at_the_start_not_on_every_update():
    cfg = make_config(tile_button_url="https://bambuddy.example/print",
                      tile_button_title="Open")
    start = build_content(_printing(), cfg, initial=True)
    assert start["button"]["url"] == "https://bambuddy.example/print"
    assert start["button"]["title"] == "Open"
    update = build_content(_printing(progress=50.0), cfg, initial=False)
    assert "button" not in update, "a button never changes; resending it is bytes on a constant"
