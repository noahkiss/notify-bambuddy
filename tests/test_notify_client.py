# ---------------------------------------------------------------------------
# The HTTP client, against a real local HTTP server.
#
# A real server rather than a mocked `urlopen`, because the things worth
# testing here are exactly the things a mock would paper over: whether the
# token really lands in the query string, whether a DELETE really carries a
# body, whether an error response body is really read back off the connection
# before the exception is raised.
#
# The server is bound to 127.0.0.1 on an ephemeral port and torn down after
# each module. Nothing reaches the internet.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from typing import Optional

import pytest

from notify_bambuddy.notify_client import (
    NotifyAmbiguous,
    NotifyError,
    NotifyAuthError,
    NotifyClient,
    NotifyDeviceNotReady,
    NotifyDisabled,
    NotifyGone,
    NotifyDeliveryFailed,
    NotifyRateLimited,
    NotifyThrottled,
    NotifyTransportError,
    NotifyUpstreamError,
    NotifyValidationError,
)

DEVICE_ID = "ABC12345"
TOKEN = "XYZ789TOKEN123"

#: Filled in per test: (status, body) the next request should get.
NEXT_RESPONSE: list = [(200, {"success": True})]
#: Every request the server saw, for assertions.
REQUESTS: list = []


class Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        REQUESTS.append({
            "method": self.command,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "body": json.loads(raw.decode()) if raw else None,
            "content_type": self.headers.get("Content-Type"),
            "user_agent": self.headers.get("User-Agent"),
        })
        status, body = NEXT_RESPONSE[0][:2]
        extra_headers = NEXT_RESPONSE[0][2] if len(NEXT_RESPONSE[0]) > 2 else {}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _handle

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def client(server) -> NotifyClient:
    REQUESTS.clear()
    NEXT_RESPONSE[0] = (200, {"success": True})
    return NotifyClient(server, DEVICE_ID, TOKEN, timeout=5.0)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def respond(status: int, body: dict, headers: Optional[dict] = None) -> None:
    NEXT_RESPONSE[0] = (status, body, headers or {})


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_a_start_addresses_the_device_and_forces_a_new_tile(client):
    """Always `new=1`. Letting the device address upsert would silently UPDATE
    another printer's tile whenever exactly one is live, and 409 whenever two
    or more are. With several printers that is the normal state of affairs."""
    respond(200, {"success": True, "activityId": "LA7Q2ZKM",
                  "expiresAt": "2026-08-10T23:24:00.000Z"})
    started = run(client.start({"title": "X1C-1", "progress": 0}))

    assert started.activity_id == "LA7Q2ZKM"
    request = REQUESTS[-1]
    assert request["method"] == "POST"
    assert request["path"] == f"/live-activity/{DEVICE_ID}"
    assert request["query"]["token"] == [TOKEN]
    assert request["query"]["new"] == ["1"]
    assert request["content_type"] == "application/json"
    assert request["user_agent"].startswith("notify-bambuddy/")


def test_an_update_addresses_the_activity_not_the_device(client):
    run(client.update("LA7Q2ZKM", {"progress": 94}))
    request = REQUESTS[-1]
    assert request["path"] == "/live-activity/LA7Q2ZKM"
    assert request["body"] == {"progress": 94}
    assert "new" not in request["query"]


def test_ending_carries_the_final_content_and_the_linger_time(client):
    run(client.end("LA7Q2ZKM", {"progress": 100, "status": "Done"}, keep_for=300))
    request = REQUESTS[-1]
    assert request["method"] == "DELETE"
    assert request["body"]["keepFor"] == 300
    assert request["body"]["status"] == "Done"


def test_a_status_read_sends_nothing_but_the_token(client):
    """A GET carrying content parameters ACTS instead of reporting. Only a
    bare GET is a query, so there must be no other parameter on this call."""
    respond(200, {"activityId": "LA7Q2ZKM", "state": "active", "endReason": None})
    run(client.status("LA7Q2ZKM"))
    request = REQUESTS[-1]
    assert request["method"] == "GET"
    assert list(request["query"].keys()) == ["token"]
    assert request["body"] is None


def test_listing_a_device_returns_only_live_tiles(client):
    respond(200, {"activities": [
        {"activityId": "LA000001", "state": "active"},
        {"activityId": "LA000002", "state": "starting"},
    ]})
    activities = run(client.list_device())
    assert {a.activity_id for a in activities} == {"LA000001", "LA000002"}
    assert all(a.is_live for a in activities)
    # `starting` is live but NOT confirmed: the push was accepted, the phone
    # has not reported back, and the tile may never appear.
    assert not [a for a in activities if a.activity_id == "LA000002"][0].is_confirmed


def test_an_alert_uses_the_json_endpoint_and_threads_by_printer(client):
    run(client.notify(text="Benchy finished on X1C-1.", title="Print finished",
                      group_type="bambuddy-00M09C411500579", time_sensitive=True))
    request = REQUESTS[-1]
    assert request["path"] == f"/notify-json/{DEVICE_ID}"
    assert request["body"]["groupType"] == "bambuddy-00M09C411500579"
    assert request["body"]["timeSensitive"] is True


def test_a_non_time_sensitive_alert_omits_the_field_entirely(client):
    run(client.notify(text="done", title="Print finished", group_type="g"))
    assert "timeSensitive" not in REQUESTS[-1]["body"]


# ---------------------------------------------------------------------------
# Error mapping
#
# The status code alone is never enough. Each of these becomes a distinct type
# because the caller's correct reaction differs.
# ---------------------------------------------------------------------------


def test_the_tile_cap_is_the_one_retryable_four_hundred(client):
    """Every other 400 is permanent. This one frees up on its own, and a
    client that treats it as permanent writes off the sixth printer in a farm
    forever."""
    respond(400, {"error": "Bad Request",
                  "message": "This device already has the maximum of 5 live activities"})
    with pytest.raises(NotifyValidationError) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.is_tile_cap
    assert caught.value.retryable


def test_an_ordinary_four_hundred_is_not_retryable(client):
    respond(400, {"error": "Bad Request",
                  "message": 'Field "endsIn" must be a positive integer number of seconds'})
    with pytest.raises(NotifyValidationError) as caught:
        run(client.start({"title": "x"}))
    assert not caught.value.is_tile_cap
    assert not caught.value.retryable


def test_a_forbidden_response_never_reveals_whether_the_id_exists(client):
    respond(403, {"error": "Forbidden", "message": "Invalid token or id not found"})
    with pytest.raises(NotifyAuthError):
        run(client.update("LA7Q2ZKM", {"progress": 1}))


def test_a_conflict_without_activity_ids_means_the_device_is_not_ready(client):
    respond(409, {"error": "Conflict",
                  "message": "Live Activities are turned off in Settings"})
    with pytest.raises(NotifyDeviceNotReady):
        run(client.start({"title": "x"}))


def test_a_conflict_carrying_activity_ids_is_the_ambiguity_case(client):
    respond(409, {"error": "Conflict", "message": "This device has 2 live activities",
                  "activityIds": ["LA000001", "LA000002"]})
    with pytest.raises(NotifyAmbiguous) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.activity_ids == ("LA000001", "LA000002")


def test_a_gone_response_carries_no_reason_until_it_is_classified(client):
    """The 410 body is prose. The machine-readable `endReason` only comes from
    the status endpoint, which is why `classify_gone` exists."""
    respond(410, {"error": "Gone", "message": "This Live Activity has ended (overdue)"})
    with pytest.raises(NotifyGone) as caught:
        run(client.update("LA7Q2ZKM", {"progress": 1}))
    assert caught.value.end_reason is None
    assert not caught.value.user_dismissed


def test_classifying_a_gone_response_distinguishes_dismissal_from_reaping(client):
    respond(410, {"error": "Gone", "message": "gone"})
    try:
        run(client.update("LA7Q2ZKM", {"progress": 1}))
        raise AssertionError("expected a 410")
    except NotifyGone as error:
        respond(200, {"activityId": "LA7Q2ZKM", "state": "ended", "endReason": "overdue"})
        reaped = run(client.classify_gone("LA7Q2ZKM", error))
        assert reaped.end_reason == "overdue"
        assert not reaped.user_dismissed

        respond(200, {"activityId": "LA7Q2ZKM", "state": "dismissed",
                      "endReason": "dismissed"})
        dismissed = run(client.classify_gone("LA7Q2ZKM", error))
        assert dismissed.user_dismissed


def test_an_unclassifiable_gone_does_not_suppress_future_tiles(client):
    """When the reason cannot be established, err toward NOT suppressing. A
    missing tile the user wanted is a bug report; a silently suppressed
    printer looks like the integration is simply broken."""
    respond(410, {"error": "Gone", "message": "gone"})
    try:
        run(client.update("LA7Q2ZKM", {"progress": 1}))
        raise AssertionError("expected a 410")
    except NotifyGone as error:
        respond(403, {"error": "Forbidden", "message": "Invalid token or id not found"})
        assert not run(client.classify_gone("LA7Q2ZKM", error)).user_dismissed


def test_a_throttle_reports_how_long_to_wait(client):
    respond(429, {"error": "Too Many Requests", "message": "starts are being ignored",
                  "retryAfterSeconds": 1800, "unansweredStarts": 2})
    with pytest.raises(NotifyThrottled) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.retry_after == 1800


def test_a_429_without_a_wait_in_its_body_is_not_the_start_throttle(client):
    """The gateway's own start throttle always says how long to wait. A 429
    that does not is a limit in front of the gateway, applying to this host,
    and treating it as the throttle sent users to open the app for nothing and
    blocked starts for half an hour."""
    respond(429, {"error": "Too Many Requests"}, headers={"Retry-After": "45"})
    with pytest.raises(NotifyRateLimited) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.retry_after == 45
    assert not isinstance(caught.value, NotifyThrottled)


def test_a_bare_429_with_no_retry_after_waits_a_minute(client):
    respond(429, {"error": "Too Many Requests"})
    with pytest.raises(NotifyRateLimited) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.retry_after == 60


def test_the_start_throttle_carries_the_gateways_own_advice(client):
    """Whether opening the app can help depends on whether a start is still
    pending, and only the gateway knows. Its message says which."""
    respond(429, {"error": "Too Many Requests", "retryAfterSeconds": 1200,
                  "openingTheAppMayHelp": False,
                  "message": "Opening the app will not shortcut this: there is no "
                             "pending tile left to retry. Please wait about 20 minute(s)."})
    with pytest.raises(NotifyThrottled) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.retry_after == 1200
    assert caught.value.opening_app_may_help is False
    assert "will not shortcut" in str(caught.value)


def test_a_bad_gateway_that_was_not_delivered_is_safe_to_retry(client):
    """`not-delivered` means the gateway deleted its row: nothing exists, so
    starting again cannot duplicate anything. No `retryAfterSeconds` is the
    gateway saying the identical request will fail identically."""
    respond(502, {"error": "Bad Gateway", "deliveryState": "not-delivered",
                  "message": "Apple rejected the Live Activity start"})
    with pytest.raises(NotifyUpstreamError) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.delivery_state == "not-delivered"
    assert caught.value.activity_id is None
    assert caught.value.retry_after is None


def test_a_bad_gateway_with_unknown_delivery_hands_back_the_tile_id(client):
    """Apple never answered, so the tile may be appearing. The gateway keeps
    the row and returns its id, which must not be thrown away."""
    respond(502, {"error": "Bad Gateway", "deliveryState": "unknown",
                  "activityId": "LA7Q2ZKM",
                  "message": "Apple could not be reached and delivery is unknown."})
    with pytest.raises(NotifyUpstreamError) as caught:
        run(client.start({"title": "x"}))
    assert caught.value.delivery_state == "unknown"
    assert caught.value.activity_id == "LA7Q2ZKM"


@pytest.mark.parametrize("apns_status, says", [
    ("DEAD_SUBSCRIPTION", "Notify web app"),
    ("DEAD_TOKEN", "Open the Notify app"),
    ("MAC_TOPIC_UNSET", "Mac devices"),
    (410, "no longer registered"),
])
def test_an_undeliverable_alert_says_what_to_do(client, apns_status, says):
    """Every delivery failure on /notify-json is a 400 reading only "Failed to
    send notification", with the reason in apnsStatus. For a browser, alerts
    are the only output, so a retired subscription silently failed every one
    and the log never said to reopen the web app."""
    respond(400, {"success": False, "error": "Failed to send notification",
                  "apnsStatus": apns_status, "apnsError": {"reason": "Unregistered"}
                  if apns_status == 410 else "detail"})
    with pytest.raises(NotifyDeliveryFailed) as caught:
        run(client.notify("text", "title", "printer"))
    assert says in str(caught.value)
    assert "Failed to send notification" not in str(caught.value)


def test_an_ordinary_400_is_still_a_validation_error(client):
    """Only a body carrying the delivery fields is a delivery failure."""
    respond(400, {"error": "Bad Request", "message": "title too long"})
    with pytest.raises(NotifyValidationError):
        run(client.notify("text", "title", "printer"))


def test_the_kill_switch_is_transient(client):
    respond(503, {"error": "Service Unavailable",
                  "message": "Live Activities are disabled"})
    with pytest.raises(NotifyDisabled) as caught:
        run(client.update("LA7Q2ZKM", {"progress": 1}))
    assert caught.value.retryable


def test_an_unreachable_gateway_is_a_transport_error(server):
    """A timeout on a START is ambiguous: the tile may exist and only the
    response was lost. The caller reconciles rather than retrying blindly."""
    offline = NotifyClient("http://127.0.0.1:1", DEVICE_ID, TOKEN, timeout=1.0)
    with pytest.raises(NotifyTransportError):
        run(offline.start({"title": "x"}))


def test_dry_run_sends_no_write_requests(server):
    client = NotifyClient(server, DEVICE_ID, TOKEN, dry_run=True)
    REQUESTS.clear()
    run(client.start({"title": "x"}))
    run(client.update("LA000001", {"progress": 5}))
    assert REQUESTS == []


def test_every_notification_carries_the_service_icon():
    """The avatar is what makes an alert recognisable in Notification Centre.

    Asserted on the payload rather than on the constant so that dropping the
    field from the body fails here, which is the mistake that would actually
    happen.
    """
    from notify_bambuddy.notify_client import NOTIFY_ICON_URL

    client = NotifyClient("https://example.invalid", "ABC12345", "tok", dry_run=True)
    sent = []
    client._request = lambda method, url, payload: _record(sent, payload)
    run(client.notify("done", "Print finished", "printer-1"))
    assert sent[0]["iconUrl"] == NOTIFY_ICON_URL
    assert NOTIFY_ICON_URL.startswith("https://"), "Notify requires HTTPS for iconUrl"


async def _record(sink, payload):
    sink.append(payload)
    return {}


def test_a_wrong_credential_is_an_auth_error_even_though_link_answers_404():
    """Regression: a typo'd device id or token started the service anyway.

    `/link` reports invalid credentials with HTTP 404 and a `success: false`
    body, not with the 403 the Live Activity endpoints use. Only 403 was
    classified as an auth error, so the startup check fell through to the
    "could not reach Notify, continuing" branch and the service ran forever
    producing nothing. Confirmed against the live gateway.
    """
    client = NotifyClient("https://example.invalid", "ABCD1234", "wrong")

    async def _404(method, url, payload):
        raise NotifyError(
            "No device or group found with the provided ID and token combination",
            404,
            {"success": False, "error": "Invalid credentials", "code": "NOT_FOUND"},
        )

    client._request = _404
    with pytest.raises(NotifyAuthError):
        run(client.link())


def test_a_success_false_body_is_an_auth_error_even_with_a_200():
    client = NotifyClient("https://example.invalid", "ABCD1234", "wrong")

    async def _ok_but_false(method, url, payload):
        return {"success": False, "message": "Invalid credentials"}

    client._request = _ok_but_false
    with pytest.raises(NotifyAuthError):
        run(client.link())


def test_a_genuine_outage_is_still_not_an_auth_error():
    """A 502 must stay retryable, or a gateway blip becomes a refusal to start."""
    client = NotifyClient("https://example.invalid", "ABCD1234", "right")

    async def _502(method, url, payload):
        raise NotifyError("bad gateway", 502, {})

    client._request = _502
    with pytest.raises(NotifyError) as caught:
        run(client.link())
    assert not isinstance(caught.value, NotifyAuthError)
