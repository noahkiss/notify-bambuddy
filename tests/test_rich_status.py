"""The richer status route, and everything it makes possible.

BambuBuddy serves printer status on two routes. The advertised `/webhook/` one
reports seven fields; its own `/api/v1/printers/` one reports sixty-three, to
the same API key. Polling the second is opt-in, must degrade when refused, and
must leave every existing deployment untouched.
"""

import json
import pathlib

from notify_bambuddy.bambuddy_api import status_to_event, _hms_tuple, _whole
from notify_bambuddy.content import build_content, hms_reason, status_word
from notify_bambuddy.models import Delivery, Phase, SessionState

from conftest import SERIAL, make_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RICH = json.loads((FIXTURES / "rich_status_running.json").read_text())

RUNOUT = {"full_code": "0C00200000020001", "severity": 2,
          "description": "Filament ran out. Please load new filament."}
NOZZLE = {"full_code": "0700400000010002", "severity": 1,
          "description": "Nozzle temperature is abnormal."}


def _session(**overrides):
    values = dict(
        session_key=f"{SERIAL}:job", serial=SERIAL, printer_name="X1C-1",
        phase=Phase.PRINTING, delivery=Delivery.ACTIVE, activity_id="LA1",
        job_name="mascot.3mf", progress=61.0, eta_seconds=2820,
        progress_trustworthy=True, created_at=1000.0,
        temperatures=RICH["temperatures"], layer_num=130, total_layers=312,
    )
    values.update(overrides)
    return SessionState(**values)


# ---------------------------------------------------------------------------
# One parser, two payload shapes
# ---------------------------------------------------------------------------


def test_the_rich_payload_is_parsed_from_a_real_response():
    """Against a response captured from a live BambuBuddy, not a hand-written
    dict that agrees with my assumptions."""
    event = status_to_event(RICH, expected_id=1)
    assert event.layer_num == 130 and event.total_layers == 312
    assert event.stage_name == "Cleaning nozzle tip"
    assert event.temperatures["nozzle"] == 215.1
    assert event.state == "RUNNING"


def test_the_webhook_payload_still_parses_and_reports_nothing_extra():
    """The seven-field shape must be unchanged: zero layers, no temperatures,
    no faults. Those are the values every consumer already reads as unknown."""
    event = status_to_event({
        "id": 1, "name": "X1C", "connected": True, "state": "RUNNING",
        "current_print": "mascot", "progress": 50.0, "remaining_time": 30,
    }, expected_id=1)
    assert (event.layer_num, event.total_layers) == (0, 0)
    assert event.temperatures is None
    assert event.stage_name is None
    assert event.hms_errors == ()


def test_a_fault_with_no_identifier_is_discarded():
    """`full_code` is what identifies a fault. Without it the raise/clear diff
    has nothing stable to compare, so the same fault would be reported as new
    on every poll."""
    assert _hms_tuple([{"severity": 1, "description": "Something"}]) == ()
    assert _hms_tuple("not a list") == ()
    assert _hms_tuple([{"full_code": "ABC", "severity": 2}])[0]["description"] is None


def test_nonsense_numbers_degrade_to_unknown_rather_than_raising():
    assert _whole(None) == 0 and _whole(-5) == 0 and _whole("12") == 0
    assert _whole(True) == 0, "a bool is not a layer count"
    assert _whole(312) == 312


# ---------------------------------------------------------------------------
# The pause that says why
# ---------------------------------------------------------------------------


def test_a_pause_says_why_when_the_printer_has_said_why():
    assert status_word(_session(phase=Phase.PAUSED, hms_errors=(RUNOUT,))) == "Filament ran out"


def test_the_reason_is_the_first_sentence_not_a_truncation():
    """The catalogue ships sentences and the slot holds forty characters.
    "Filament ran out." is a complete thought; "Filament ran out. Please load
    new fil..." is not."""
    assert "Please load" not in hms_reason(_session(hms_errors=(RUNOUT,)))
    assert not hms_reason(_session(hms_errors=(RUNOUT,))).endswith(".")


def test_the_worst_fault_wins():
    """Severity 1 is fatal, 4 is informational, so the LOWEST number wins."""
    assert hms_reason(_session(hms_errors=(RUNOUT, NOZZLE))) == "Nozzle temperature is abnormal"


def test_an_uncatalogued_fault_leaves_the_generic_word():
    """BambuBuddy's catalogue does not cover every code and returns null for
    many. A raw hex code on a Lock Screen tells nobody anything."""
    bare = {"full_code": "DEADBEEF", "severity": 1, "description": None}
    assert status_word(_session(phase=Phase.PAUSED, hms_errors=(bare,))) == "Paused"


def test_a_pause_with_no_fault_reads_as_before():
    assert status_word(_session(phase=Phase.PAUSED)) == "Paused"


# ---------------------------------------------------------------------------
# Chips, including the temperatures
# ---------------------------------------------------------------------------


def test_temperature_chips_show_the_target_when_the_printer_is_driving_to_one():
    c = build_content(_session(), make_config(tile_metric_fields=("nozzle", "bed")),
                      initial=True)
    assert c["metrics"] == [
        {"label": "Nozzle", "value": "215/215", "unit": "C"},
        {"label": "Bed", "value": "60/60", "unit": "C"},
    ]


def test_a_cold_printer_shows_one_number_not_a_slash_zero():
    """A zero target means "not heating", and "24/0" on every idle tile is
    noise pretending to be information."""
    cold = {"nozzle": 24.3, "nozzle_target": 0.0}
    c = build_content(_session(temperatures=cold),
                      make_config(tile_metric_fields=("nozzle",)), initial=True)
    assert c["metrics"] == [{"label": "Nozzle", "value": "24", "unit": "C"}]


def test_chips_appear_in_the_order_they_were_asked_for():
    c = build_content(_session(), make_config(
        tile_metric_fields=("bed", "progress", "nozzle")), initial=True)
    assert [m["label"] for m in c["metrics"]] == ["Bed", "Progress", "Nozzle"]


def test_a_chip_with_no_data_is_dropped_not_shown_as_zero():
    """Asking for temperatures on a webhook-only install must shorten the row,
    never fill it with noughts."""
    c = build_content(_session(temperatures=None),
                      make_config(tile_metric_fields=("progress", "nozzle")), initial=True)
    assert c["metrics"] == [{"label": "Progress", "value": "61%"}]


def test_temperatures_alone_on_a_webhook_install_give_back_the_model_name():
    """`metrics` would otherwise be an empty list, which says "show no chips"
    rather than "leave the body line alone"."""
    c = build_content(_session(temperatures=None),
                      make_config(tile_metric_fields=("nozzle", "bed")), initial=True)
    assert c["metrics"] is None
    assert c["body"] == "mascot.3mf"


def test_the_layer_chip_needs_both_halves():
    c = build_content(_session(total_layers=0),
                      make_config(tile_metric_fields=("layers",)), initial=True)
    assert c["metrics"] is None


# ---------------------------------------------------------------------------
# The stage name
# ---------------------------------------------------------------------------


def test_the_stage_name_is_opt_in():
    s = _session(stage_name="Cleaning nozzle tip")
    assert status_word(s, make_config()) == "Printing"
    assert status_word(s, make_config(tile_stage=True)) == "Cleaning nozzle tip"


def test_a_missing_stage_falls_back_rather_than_blanking_the_slot():
    assert status_word(_session(stage_name=None), make_config(tile_stage=True)) == "Printing"


# ---------------------------------------------------------------------------
# The condition alerts
# ---------------------------------------------------------------------------


from conftest import build, run, status  # noqa: E402


def _alerting(**cfg):
    config = make_config(with_alerts=True, **cfg) if False else make_config(**cfg)
    coord, notify, _ = build(config=config, with_alerts=True)
    return coord, notify


def _texts(notify):
    return [n.get("body") or n.get("text") or "" for n in notify.notifications]


def test_nothing_new_is_sent_by_default(clock):
    """Every condition alert is opt-in. Upgrading must not start pushing
    notifications nobody asked for."""
    coord, notify = _alerting()
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, connected=False, state="PAUSE",
                            hms_errors=(RUNOUT,), awaiting_plate_clear=True)))
    assert not _texts(notify), f"sent unrequested alerts: {_texts(notify)}"


def test_a_fault_is_announced_once_not_on_every_poll(clock):
    """The same fault is republished on every reading. Only its arrival is
    news."""
    coord, notify = _alerting(bambuddy_rich_status=True, alert_hms="serious")
    run(coord.handle(status(clock)))
    for _ in range(4):
        clock.advance(30)
        run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,))))
    hits = [t for t in _texts(notify) if "Filament" in t]
    assert len(hits) == 1, f"announced the same fault {len(hits)} times"


def test_an_informational_fault_is_filtered_but_still_remembered(clock):
    """Filtering must not leave the fault looking new forever: if the known set
    were only updated for faults that passed the filter, a severity-4 condition
    would be rediscovered on every single poll."""
    info = {"full_code": "0500400000000009", "severity": 4, "description": "Minor thing."}
    coord, notify = _alerting(bambuddy_rich_status=True, alert_hms="serious")
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, hms_errors=(info,))))
    assert not [t for t in _texts(notify) if "Minor" in t]
    assert coord._sessions[SERIAL].known_hms == {"0500400000000009"}


def test_offline_is_not_announced_on_the_very_first_reading(clock):
    """A printer that was already offline has not just gone offline. Without
    this every restart would announce one."""
    coord, notify = _alerting(alert_offline=True)
    run(coord.handle(status(clock)))
    run(coord.handle(status(clock, offset=1)))
    assert not [t for t in _texts(notify) if "offline" in t.lower()]


def test_going_offline_and_coming_back_are_both_announced(clock):
    coord, notify = _alerting(alert_offline=True)
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, connected=False)))
    clock.advance(30)
    run(coord.handle(status(clock, connected=True)))
    said = " | ".join(_texts(notify)).lower()
    assert "went offline" in said and "back online" in said


def test_a_blocked_plate_is_announced_once_and_can_re_arm(clock):
    coord, notify = _alerting(alert_plate_clear=True)
    run(coord.handle(status(clock)))
    for _ in range(3):
        clock.advance(30)
        run(coord.handle(status(clock, awaiting_plate_clear=True)))
    assert len([t for t in _texts(notify) if "plate" in t.lower()]) == 1


def test_a_pause_alert_carries_the_reason_and_a_resume_closes_it(clock):
    coord, notify = _alerting(bambuddy_rich_status=True, alert_pause=True)
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,))))
    clock.advance(30)
    run(coord.handle(status(clock, state="RUNNING")))
    said = " | ".join(_texts(notify))
    assert "Filament ran out" in said, f"pause alert lost its reason: {said}"
    assert "Resumed" in said


# ---------------------------------------------------------------------------
# The route falls back rather than failing
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

from notify_bambuddy.bambuddy_api import APISource, BambuBuddyAuthError, BambuBuddyError  # noqa: E402


class _StubClient:
    """Counts which route was asked, and can refuse the rich one."""

    def __init__(self, rich_error=None):
        self.rich_calls = 0
        self.plain_calls = 0
        self._rich_error = rich_error

    async def rich_status(self, printer_id):
        self.rich_calls += 1
        if self._rich_error is not None:
            raise self._rich_error
        return {"id": printer_id, "name": "X", "connected": True, "state": "RUNNING",
                "progress": 1.0, "remaining_time": 1, "layer_num": 5, "total_layers": 10}

    async def status(self, printer_id):
        self.plain_calls += 1
        return {"id": printer_id, "name": "X", "connected": True, "state": "RUNNING",
                "progress": 1.0, "remaining_time": 1}


def _source(client, **cfg):
    loop = asyncio.new_event_loop()
    src = APISource(make_config(**cfg), loop, asyncio.Queue(maxsize=8))
    src._client = client
    return src, loop


def test_the_rich_route_is_not_touched_unless_asked_for():
    client = _StubClient()
    src, loop = _source(client, bambuddy_rich_status=False)
    try:
        loop.run_until_complete(src._fetch_status(1))
    finally:
        loop.close()
    assert (client.rich_calls, client.plain_calls) == (0, 1)


def test_a_refusal_retires_the_rich_route_for_the_whole_run():
    """A 403 is a statement about how this BambuBuddy is configured, not a
    blip. Retrying it every ten seconds would spend a request per poll forever
    to be told the same thing."""
    client = _StubClient(rich_error=BambuBuddyAuthError("forbidden"))
    src, loop = _source(client, bambuddy_rich_status=True)
    try:
        for _ in range(5):
            loop.run_until_complete(src._fetch_status(1))
    finally:
        loop.close()
    assert client.rich_calls == 1, f"kept retrying a refused route {client.rich_calls} times"
    assert client.plain_calls == 5, "every poll must still be answered"


def test_an_outage_on_the_rich_route_is_not_silently_downgraded():
    """A timeout or a 500 is an outage. Swallowing it would quietly move the
    whole service onto poorer data and report nothing wrong."""
    client = _StubClient(rich_error=BambuBuddyError("connection reset"))
    src, loop = _source(client, bambuddy_rich_status=True)
    try:
        raised = False
        try:
            loop.run_until_complete(src._fetch_status(1))
        except BambuBuddyError:
            raised = True
    finally:
        loop.close()
    assert raised, "an outage was hidden behind the fallback"
    assert client.plain_calls == 0


# ---------------------------------------------------------------------------
# Everything an adversarial review found in the first cut of this feature
# ---------------------------------------------------------------------------

ADVISORY = {"full_code": "0C00030000020003", "severity": 4,
            "description": "AMS humidity is high. Consider drying the filament."}


def test_a_blocked_plate_is_actually_read_off_the_payload():
    """ALERT_PLATE_CLEAR could never fire: the alert, the config and the docs
    all existed, and `status_to_event` threw the field away. The alert's own
    test passed because the fixture set the field directly, bypassing the
    parser that production uses."""
    event = status_to_event({
        "id": 1, "name": "X", "connected": True, "state": "FINISH",
        "progress": 100.0, "remaining_time": 0, "awaiting_plate_clear": True,
    }, expected_id=1, rich=True)
    assert event.awaiting_plate_clear is True


def test_an_advisory_does_not_hijack_the_pause_word():
    """A severity-4 AMS advisory can sit active for a whole print. Letting it
    describe a pause told someone who paused the printer themselves that their
    filament was damp, and made a deliberate pause look like an incident."""
    assert status_word(_session(phase=Phase.PAUSED, hms_errors=(ADVISORY,))) == "Paused"
    assert status_word(_session(phase=Phase.PAUSED, hms_errors=(ADVISORY, RUNOUT))) \
        == "Filament ran out"


def test_the_layer_chip_refuses_a_counter_past_its_own_total():
    """BambuBuddy keeps the previous print's denominator until the firmware
    republishes, so a new print transiently reads 57/40. The trailing slot
    already refused that; the chip repeated two of its three checks and so
    rendered it."""
    c = build_content(_session(layer_num=57, total_layers=40),
                      make_config(tile_metric_fields=("layers",)), initial=True)
    assert c["metrics"] is None


def test_a_missing_printers_route_degrades_instead_of_stopping_the_service():
    """A 404 is a route that is not there: an older BambuBuddy, or a proxy
    forwarding only /api/v1/webhook/. Treated as an outage it stopped the poll
    loop at the first printer, and the service went silent while reporting
    healthy."""
    from notify_bambuddy.bambuddy_api import BambuBuddyMissingRoute
    client = _StubClient(rich_error=BambuBuddyMissingRoute("HTTP 404 from /1/status"))
    src, loop = _source(client, bambuddy_rich_status=True)
    try:
        for _ in range(3):
            loop.run_until_complete(src._fetch_status(1))
    finally:
        loop.close()
    assert client.rich_calls == 1 and client.plain_calls == 3


def test_no_all_clear_from_a_frame_that_could_not_have_carried_faults(clock):
    """After the source falls back, every frame reports no faults. Reading that
    as "the fault went away" told users a filament runout was resolved while
    the spool was still empty. The config flag still says rich; only the frame
    knows."""
    coord, notify = _alerting(bambuddy_rich_status=True, alert_hms="serious")
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,))))
    clock.advance(30)
    # The fallback happened: same printer, same empty-handed webhook shape.
    run(coord.handle(status(clock, state="PAUSE", rich=False)))
    assert not [t for t in _texts(notify) if "cleared" in t.lower()], (
        "announced an all-clear from a route that cannot report faults"
    )


def test_no_all_clear_for_a_fault_nobody_was_told_about(clock):
    """`known_hms` deliberately records filtered-out faults so they are not
    rediscovered every poll. Diffing the all-clear against it meant a
    severity-4 advisory produced a "Fault cleared" push when it went away."""
    coord, notify = _alerting(bambuddy_rich_status=True, alert_hms="serious")
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, hms_errors=(ADVISORY,))))
    clock.advance(30)
    run(coord.handle(status(clock, hms_errors=(), rich=True)))
    assert not _texts(notify), f"orphan all-clear: {_texts(notify)}"


def test_a_standing_fault_is_not_re_announced_when_a_session_opens(clock):
    """A restart reloads the session and the printer is still faulted. Without
    seeding, the next frame diffs a months-old fault against an empty set and
    pushes a Time Sensitive alert at 3am for something already known."""
    coord, notify = _alerting(bambuddy_rich_status=True)
    run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,))))
    clock.advance(30)
    run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,))))
    assert not [t for t in _texts(notify) if "Filament" in t], (
        "re-announced a fault that was already active when the session opened"
    )


def test_the_new_alerts_respect_the_do_not_disturb_setting(clock):
    """ALERTS_TIME_SENSITIVE=0 is someone saying this service must not pierce a
    Focus mode. The condition alerts were added sending it directly."""
    coord, notify = _alerting(bambuddy_rich_status=True, alert_pause=True,
                              alert_offline=True, alerts_time_sensitive=False)
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,))))
    clock.advance(30)
    run(coord.handle(status(clock, connected=False)))
    urgent = [n for n in notify.notifications if n["timeSensitive"]]
    assert not urgent, f"broke Do Not Disturb against the setting: {urgent}"


def test_switching_off_a_finished_printer_is_not_an_incident(clock):
    """Taking the plate off and switching the printer off is the ordinary end
    of a job. The session is retired moments later, so the matching "back
    online" could never arrive to close the loop."""
    coord, notify = _alerting(alert_offline=True)
    run(coord.handle(status(clock)))
    for _ in range(4):
        clock.advance(60)
        run(coord.handle(status(clock, state="FINISH", progress=100.0)))
    clock.advance(30)
    run(coord.handle(status(clock, state="FINISH", connected=False)))
    assert not [t for t in _texts(notify) if "offline" in t.lower()]


def test_an_opt_in_alert_is_not_vetoed_by_the_alerts_level(clock):
    """ALERTS=problems is the natural choice for someone who does not want
    completion pings. It silently dropped every resume, and every pause with no
    stated cause, forever."""
    coord, notify = _alerting(alerts="problems", alert_pause=True)
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, state="PAUSE")))
    clock.advance(30)
    run(coord.handle(status(clock, state="RUNNING")))
    said = " | ".join(_texts(notify))
    assert "Paused" in said and "Resumed" in said, f"ALERTS=problems ate them: {said}"


def test_alerts_off_still_silences_everything(clock):
    """The one level that outranks an explicit opt-in."""
    coord, notify = _alerting(alerts="off", bambuddy_rich_status=True,
                              alert_pause=True, alert_offline=True,
                              alert_plate_clear=True, alert_hms="all")
    run(coord.handle(status(clock)))
    clock.advance(30)
    run(coord.handle(status(clock, state="PAUSE", hms_errors=(RUNOUT,),
                            connected=False, awaiting_plate_clear=True)))
    assert not _texts(notify)
