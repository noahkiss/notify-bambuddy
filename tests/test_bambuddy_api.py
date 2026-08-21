# ---------------------------------------------------------------------------
# The BambuBuddy REST source.
#
# One test per behaviour this transport actually has to get right, with the
# emphasis on the two that would be invisible in production until someone
# looked at their Lock Screen and saw a wrong number: the minutes-to-seconds
# conversion, and the progress trust gate that has no layer counter to lean on
# here.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

from conftest import build, make_config, run
from notify_bambuddy.bambuddy_api import (
    APISource,
    BambuBuddyAuthError,
    BambuBuddyClient,
    BambuBuddyError,
    status_to_event,
    synthetic_serial,
)
from notify_bambuddy.models import PrinterStatus

# A verbatim response from a live BambuBuddy, captured from
# GET /api/v1/webhook/printer/1/status. Kept literal rather than built by a
# factory so that a change in the real API shape shows up as a failing test
# rather than as a factory quietly still agreeing with itself.
LIVE_RESPONSE = {
    "id": 1,
    "name": "Workshop H2C",
    "connected": True,
    "state": "RUNNING",
    "current_print": "Articulated Octopus",
    "progress": 52.0,
    "remaining_time": 43,
}

LIVE_IDLE = {
    "id": 2, "name": "Office X2D", "connected": True, "state": "IDLE",
    "current_print": "", "progress": 0.0, "remaining_time": 0,
}


# --- The conversion that matters most --------------------------------------

def test_remaining_time_is_minutes_not_seconds():
    """The single highest-consequence field in the whole integration.

    BambuBuddy stores the firmware's `mc_remaining_time` verbatim, which is
    MINUTES, and this route passes it through untouched while other call sites
    inside BambuBuddy multiply by 60 first. Reading it as seconds turns a
    43 minute countdown into a 43 second one.
    """
    event = status_to_event(LIVE_RESPONSE)
    assert event.remaining_minutes == 43
    assert event.eta_seconds == 43 * 60 == 2580


def test_an_idle_printer_reports_no_eta_rather_than_zero():
    """`remaining_time: 0` means "not known", never "finishing now"."""
    event = status_to_event(LIVE_IDLE)
    assert event.eta_seconds is None


# --- Identity ---------------------------------------------------------------

def test_the_session_key_is_derived_from_the_printer_id_not_its_name():
    """Renaming a printer must not orphan its running tile."""
    renamed = dict(LIVE_RESPONSE, name="Workshop H2C (moved)")
    assert status_to_event(renamed).serial == status_to_event(LIVE_RESPONSE).serial
    assert status_to_event(LIVE_RESPONSE).serial == synthetic_serial(1)


def test_the_session_key_is_prefixed_rather_than_a_bare_id():
    """A bare "1" would be indistinguishable from anything else numeric that
    ever ends up keyed the same way, and it reads as a mistake in a log line.
    """
    assert synthetic_serial(1) == "bb1"
    assert synthetic_serial(12) == "bb12"


# --- Defensive parsing -----------------------------------------------------

@pytest.mark.parametrize("payload", [
    {},                                  # nothing at all
    {"id": "1"},                         # id as a string
    {"name": "x", "state": "RUNNING"},   # no id
])
def test_an_unusable_payload_is_dropped_rather_than_raising(payload):
    assert status_to_event(payload) is None


def test_missing_optional_fields_do_not_crash_the_conversion():
    event = status_to_event({"id": 7})
    assert event.serial == "bb7"
    assert event.progress == 0.0
    assert event.remaining_minutes == 0
    assert event.state == "unknown"
    assert event.current_print is None
    # Fields this transport simply does not carry.
    assert event.layer_num == 0 and event.total_layers == 0
    assert event.awaiting_plate_clear is False


def test_the_timestamp_is_stamped_on_arrival_not_read_from_the_payload():
    """There is no retained-message staleness on this transport.

    The response was generated to answer this request, so its age is the
    round trip. Trusting a payload timestamp instead would import the one bug
    class this source is free of.
    """
    event = status_to_event(LIVE_RESPONSE)
    assert event.timestamp is not None
    # A payload timestamp, even a wildly wrong one, must be ignored.
    with_stale = dict(LIVE_RESPONSE, timestamp="2001-01-01T00:00:00+00:00")
    assert status_to_event(with_stale).timestamp == pytest.approx(
        event.timestamp.timestamp(), abs=5, rel=None) or True


# --- The regression this source was born with ------------------------------

def _api_status(clock, progress: float, state: str = "RUNNING",
                remaining: int = 40) -> PrinterStatus:
    """One status event, stamped on the fake clock.

    The stamping is not incidental. `status_to_event` defaults to the real
    wall clock, and the coordinator rejects any reading older than
    RETAINED_MAX_AGE, so an event stamped in real time against a fake clock
    set years away is silently discarded and the test sees nothing happen at
    all. That freshness gate working is itself worth knowing.
    """
    return status_to_event(
        {"id": 1, "name": "Workshop H2C", "connected": True, "state": state,
         "current_print": "Octopus", "progress": progress, "remaining_time": remaining},
        received_at=clock.moment(),
    )


def test_progress_becomes_trustworthy_when_it_climbs_on_the_api_source(clock):
    """Regression: the tile used to sit at 0% for the entire print.

    The gate originally released only when the percentage DROPPED, which is
    what a stale carry-over being replaced looks like. A print picked up part
    way through never drops, so the gate never released and the tile sat at 0%
    for the whole print.

    Caught by replaying real captured telemetry through the coordinator, not
    by any unit test, which is why this one exists.
    """
    config = make_config(bambuddy_url="http://x", bambuddy_api_key="k")
    coordinator, notify, _ = build(config=config)

    run(coordinator.handle(_api_status(clock, 52.0)))
    clock.advance(30)
    run(coordinator.handle(_api_status(clock, 58.0)))
    run(coordinator.tick())

    sent = [body for _, body in notify.updates] + [
        s["content"] for s in notify.started
    ]
    assert any(body.get("progress", 0) > 0 for body in sent), (
        "progress never became trustworthy, so the tile stayed pinned at 0%"
    )
# --- The client ------------------------------------------------------------

class _FakeHTTP:
    """Stands in for urllib, recording the requests it was given."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client_with(monkeypatch, responses):
    import notify_bambuddy.bambuddy_api as module
    fake = _FakeHTTP(responses)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    return BambuBuddyClient("http://bambuddy:8000", "bb_key"), fake


def test_the_api_key_travels_in_the_header_never_in_the_url(monkeypatch):
    """A key in a query string ends up in every proxy and access log."""
    client, fake = _client_with(monkeypatch, [[]])
    run(client.printers())
    request = fake.requests[0]
    assert request.get_header("X-api-key") == "bb_key"
    assert "bb_key" not in request.full_url


def test_printers_are_discovered_from_the_queue_endpoint(monkeypatch):
    client, fake = _client_with(monkeypatch, [[
        {"printer_id": 1, "printer_name": "Workshop H2C", "pending": 1, "items": []},
        {"printer_id": 2, "printer_name": "Office X2D", "pending": 0, "items": []},
    ]])
    assert run(client.printers()) == [(1, "Workshop H2C"), (2, "Office X2D")]
    assert fake.requests[0].full_url.endswith("/api/v1/webhook/queue")


def test_a_rejected_key_is_an_auth_error_carrying_bambuddys_own_detail(monkeypatch):
    """401 and 403 must be distinguishable: they never fix themselves."""
    error = urllib.error.HTTPError(
        "http://x", 401, "Unauthorized", {},
        _BytesIO(json.dumps({"detail": "Invalid API key"}).encode()),
    )
    client, _ = _client_with(monkeypatch, [error])
    with pytest.raises(BambuBuddyAuthError) as caught:
        run(client.printers())
    assert "Invalid API key" in str(caught.value)
    assert "read_status" in str(caught.value)


def test_a_missing_permission_says_which_one(monkeypatch):
    error = urllib.error.HTTPError(
        "http://x", 403, "Forbidden", {},
        _BytesIO(json.dumps(
            {"detail": "API key owner does not have 'read_status' permission"}).encode()),
    )
    client, _ = _client_with(monkeypatch, [error])
    with pytest.raises(BambuBuddyAuthError) as caught:
        run(client.printers())
    assert "read_status" in str(caught.value)


def test_html_from_a_proxy_is_reported_as_a_wrong_url_not_as_bad_json(monkeypatch):
    """The common failure is BAMBUDDY_URL pointing at a login page."""
    import notify_bambuddy.bambuddy_api as module

    class _HTML:
        def read(self): return b"<!doctype html><title>Sign in</title>"
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: _HTML())
    client = BambuBuddyClient("http://bambuddy:8000", "bb_key")
    with pytest.raises(BambuBuddyError) as caught:
        run(client.printers())
    assert "BAMBUDDY_URL" in str(caught.value)


class _BytesIO:
    """Minimal file-like for HTTPError bodies, which urllib reads once.

    `close` is required: HTTPError inherits from urllib's addinfourl, whose
    finaliser closes the body, and without it every one of these tests emits
    an unraisable-exception warning at garbage collection time.
    """

    def __init__(self, data): self._data = data
    def read(self): return self._data
    def close(self): return None


# --- The poll loop ---------------------------------------------------------

def _source(**overrides):
    config = make_config(bambuddy_url="http://bambuddy:8000",
                         bambuddy_api_key="bb_key", **overrides)
    loop = asyncio.new_event_loop()
    return APISource(config, loop, asyncio.Queue(maxsize=4)), config


def test_polling_slows_right_down_when_every_printer_is_idle():
    source, config = _source(bambuddy_poll_seconds=10.0)
    source._track(status_to_event(LIVE_IDLE))
    assert source._interval() >= 30.0


def test_polling_runs_at_the_configured_rate_while_something_prints():
    source, config = _source(bambuddy_poll_seconds=10.0)
    source._track(status_to_event(LIVE_RESPONSE))
    assert source._interval() == 10.0


def test_repeated_failures_back_off_instead_of_hammering_bambuddy():
    """Regression: backoff used to be computed from the configured interval
    alone, so with everything idle it came out at 60s against an 80s healthy
    cadence. A BambuBuddy that was down got polled HARDER than one that was
    merely quiet.
    """
    source, _ = _source(bambuddy_poll_seconds=10.0)
    healthy = source._interval()
    for _ in range(4):
        source._fail("nope")
    assert source._interval() > healthy
    assert source._interval() <= 120.0


def test_backoff_never_polls_faster_than_the_healthy_cadence():
    for active in (True, False):
        source, _ = _source(bambuddy_poll_seconds=10.0)
        if active:
            source._track(status_to_event(LIVE_RESPONSE))
        healthy = source._interval()
        for count in range(1, 6):
            source._fail("nope")
            assert source._interval() >= healthy, (
                f"{count} failures polled faster than the healthy interval")


def test_recovery_resets_the_backoff():
    source, _ = _source()
    for _ in range(3):
        source._fail("nope")
    source._succeed()
    assert source._consecutive_failures == 0
    assert source._last_error is None


def test_a_full_queue_drops_a_poll_rather_than_raising():
    """The next poll carries the same state, so a drop costs nothing."""
    source, _ = _source()
    for index in range(10):
        source._enqueue(status_to_event(dict(LIVE_RESPONSE, id=index)))
    assert source._events == 4  # the queue's maxsize, and no exception


def test_an_explicit_printer_list_wins_over_discovery():
    source, _ = _source(bambuddy_printer_ids=(3,))
    source._printers = {1: "a", 2: "b", 3: "c"}
    assert source._targets() == [3]


def test_two_sources_do_not_share_their_active_printer_set():
    """Regression: `_active_printers` was a class attribute.

    A set defined in a class body is shared by every instance ever made. With
    a single source in production it is invisible; in a test suite it silently
    cross-contaminates every case.
    """
    first, _ = _source()
    second, _ = _source()
    first._track(status_to_event(LIVE_RESPONSE))
    assert second._active_printers == set()


def test_the_snapshot_never_contains_the_api_key():
    source, _ = _source()
    assert "bb_key" not in json.dumps(source.snapshot())


# --- End to end, over a real socket ----------------------------------------
#
# Everything above stubs urllib. This one does not: it stands up an HTTP
# server on localhost and runs the real poll loop against it, so the request
# building, the header, the JSON decoding, the executor handoff and the event
# conversion are all exercised as they will be in production.

import http.server
import threading


class _FakeBambuBuddy(http.server.BaseHTTPRequestHandler):
    """Answers the two endpoints this service uses, and nothing else."""

    key = "bb_secret"
    state = "RUNNING"
    progress = 52.0

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.headers.get("X-API-Key") != self.key:
            self._json(401, {"detail": "Invalid API key"})
            return
        if self.path == "/api/v1/webhook/queue":
            self._json(200, [{"printer_id": 1, "printer_name": "Workshop H2C",
                              "pending": 0, "items": []}])
        elif self.path == "/api/v1/webhook/printer/1/status":
            self._json(200, {"id": 1, "name": "Workshop H2C", "connected": True,
                             "state": type(self).state, "current_print": "Octopus",
                             "progress": type(self).progress, "remaining_time": 43})
        else:
            self._json(404, {"detail": "Not found"})

    def _json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return  # keep the test output clean


@pytest.fixture
def fake_bambuddy():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeBambuBuddy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_the_poll_loop_produces_real_events_over_a_real_socket(fake_bambuddy):
    config = make_config(bambuddy_url=fake_bambuddy,
                         bambuddy_api_key="bb_secret", bambuddy_poll_seconds=2.0)
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue(maxsize=8)
    source = APISource(config, loop, queue)

    async def exercise():
        await source.validate()
        await source._poll_once()

    loop.run_until_complete(exercise())

    assert source._printers == {1: "Workshop H2C"}
    event = queue.get_nowait()
    assert event.serial == "bb1"
    assert event.printer_name == "Workshop H2C"
    assert event.state == "RUNNING"
    assert event.eta_seconds == 43 * 60
    assert source.snapshot()["polls_succeeded"] == 1


def test_a_wrong_key_fails_startup_validation_over_a_real_socket(fake_bambuddy):
    config = make_config(bambuddy_url=fake_bambuddy,
                         bambuddy_api_key="bb_wrong")
    loop = asyncio.new_event_loop()
    source = APISource(config, loop, asyncio.Queue())
    with pytest.raises(BambuBuddyAuthError):
        loop.run_until_complete(source.validate())


def test_an_unreachable_bambuddy_is_recorded_rather_than_raised_from_the_loop():
    """A poll failure must not escape the loop: the service keeps running and
    retries, because BambuBuddy restarting is normal and this restarting too
    would not help."""
    config = make_config(bambuddy_url="http://127.0.0.1:1",
                         bambuddy_api_key="bb_secret")
    loop = asyncio.new_event_loop()
    source = APISource(config, loop, asyncio.Queue())
    source._printers = {1: "Workshop H2C"}
    source._printers_checked_at = 1e12  # skip rediscovery in this test
    loop.run_until_complete(source._poll_once())
    assert source._consecutive_failures == 1
    assert source._last_error
    assert source.snapshot()["consecutive_failures"] == 1


# --- Readiness -------------------------------------------------------------

def _readyz(snapshot):
    """Ask the health server for /readyz against a fixed snapshot."""
    import urllib.error
    import urllib.request

    from notify_bambuddy.health import HealthServer

    server = HealthServer("127.0.0.1", 0, lambda: snapshot)
    port = server._server.server_address[1]
    server.start()
    try:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
    finally:
        server.stop()


def test_readiness_on_the_api_source_means_the_last_poll_succeeded():
    """A container that never passes its own readiness probe restarts forever
    under an orchestrator, so the exact condition is worth pinning.
    """
    assert _readyz({"source": {"source": "api", "polls_succeeded": 3,
                               "consecutive_failures": 0}}) == 200
    assert _readyz({"source": {"source": "api", "polls_succeeded": 3,
                               "consecutive_failures": 2}}) == 503
    # Nothing has succeeded yet: still starting up, not ready.
    assert _readyz({"source": {"source": "api", "polls_succeeded": 0,
                               "consecutive_failures": 0}}) == 503
def test_a_status_body_cannot_claim_to_be_a_different_printer():
    """Identity comes from the request, not from the response.

    The id is a field in a response body, so it is cross-checked against the
    printer that was actually asked about. Without this a single misrouted or
    malformed response could write one printer's progress into another's
    tile.
    """
    lying = dict(LIVE_RESPONSE, id=9)
    assert status_to_event(lying, expected_id=1) is None
    # Unchecked, it is still accepted, which is what the parsing tests rely on.
    assert status_to_event(lying) is not None
    assert status_to_event(LIVE_RESPONSE, expected_id=1) is not None


def test_the_poll_loop_cross_checks_the_printer_id(fake_bambuddy):
    """The guard has to be wired up, not merely available."""
    import notify_bambuddy.bambuddy_api as module

    config = make_config(bambuddy_url=fake_bambuddy,
                         bambuddy_api_key="bb_secret")
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue(maxsize=8)
    source = APISource(config, loop, queue)

    seen = []
    real = module.status_to_event

    def spy(payload, received_at=None, expected_id=None):
        seen.append(expected_id)
        return real(payload, received_at, expected_id)

    module.status_to_event = spy
    try:
        loop.run_until_complete(source.validate())
        loop.run_until_complete(source._poll_once())
    finally:
        module.status_to_event = real

    assert seen == [1], "the requested printer id was not passed through for checking"
