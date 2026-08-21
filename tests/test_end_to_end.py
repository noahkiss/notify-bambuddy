# ---------------------------------------------------------------------------
# One whole print, end to end.
#
# Everything except BambuBuddy itself is the real thing: a real status
# response in the exact shape the REST API returns it, the real parser, the
# real coordinator, the real HTTP client, and real HTTP over localhost. The
# only stand-in is the Notify gateway on the far end, which records what it
# received.
#
# The unit tests prove each piece behaves. This proves the pieces still behave
# when wired together, which is a different question: an off-by-sixty in the
# unit conversion or a field the coordinator forgets to pass through would
# pass every unit test in the suite and still put the wrong number on someone's
# Lock Screen.
#
# The service has exactly one input, `GET /api/v1/webhook/printer/{id}/status`,
# and exactly one event, `PrinterStatus`. Every word and number a tile ever
# shows is derived from the seven keys in `CAPTURED_RESPONSE` below, so this
# file doubles as the record of how little there is to work with.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import make_config

from notify_bambuddy import coordinator as coordinator_module
from notify_bambuddy.alerts import Alerts
from notify_bambuddy.bambuddy_api import status_to_event
from notify_bambuddy.coordinator import Coordinator
from notify_bambuddy.notify_client import NotifyClient
from notify_bambuddy.store import Store

REQUESTS: list = []


# ---------------------------------------------------------------------------
# The one payload shape this service consumes.
#
# Verbatim from a real BambuBuddy, and deliberately not extended: seven keys,
# exactly these names. No serial, no layer numbers, no HMS error codes, no
# plate-clear flag. Anything a test reaches for beyond these seven does not
# exist on this transport, and a tile that appeared to depend on it would be
# depending on something the service can never actually see.
#
# `remaining_time` is MINUTES. The BambuBuddy wiki's own example implies
# seconds and is wrong, and reading it as seconds turns a 43 minute countdown
# into a 43 second one, so every ETA assertion below is written as an explicit
# multiplication rather than a bare number.
# ---------------------------------------------------------------------------
CAPTURED_RESPONSE = {
    "id": 1,
    "name": "Workshop H2C",
    "connected": True,
    "state": "RUNNING",
    "current_print": "Articulated Octopus",
    "progress": 52.0,
    "remaining_time": 43,
}


def api_status(**overrides: Any) -> dict:
    """One `GET /api/v1/webhook/printer/{id}/status` response body."""
    payload = dict(CAPTURED_RESPONSE)
    payload.update(overrides)
    return payload


class Gateway(BaseHTTPRequestHandler):
    """A minimal stand-in for push.getnotifyapp.com."""

    counter = [0]

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        query = parse_qs(parsed.query)
        body = json.loads(raw.decode()) if raw else None

        if parsed.path.startswith("/live-activity/") and self.command == "POST" \
                and query.get("new"):
            self.counter[0] += 1
            # `expiresAt` is stamped from the COORDINATOR's clock, which the
            # `clock` fixture replaces with a fake one. Stamping it from the
            # real clock would put Apple's eight-hour ceiling in the past
            # relative to the fake "now", and the rollover would end the tile
            # and start a replacement on the very next tick.
            started_at = datetime.fromtimestamp(coordinator_module.time.time(), tz=timezone.utc)
            response = {
                "success": True,
                "activityId": f"LA{self.counter[0]:06d}",
                "expiresAt": (started_at + timedelta(hours=8)).isoformat(),
            }
        elif parsed.path.startswith("/live-activity/") and self.command == "GET":
            response = {"activityId": parsed.path.rsplit("/", 1)[-1], "state": "active",
                        "endReason": None}
        else:
            response = {"success": True}

        # The response is recorded alongside the request because the activity
        # id only ever appears in it: a start is addressed to the DEVICE, so
        # nothing in the request itself says which tile was created.
        REQUESTS.append({
            "method": self.command,
            "path": parsed.path,
            "query": query,
            "body": body,
            "response": response,
        })

        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _handle

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def gateway():
    httpd = HTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def writes() -> list:
    """Only the calls that change something: starts, updates, ends, alerts."""
    return [r for r in REQUESTS if r["method"] in ("POST", "DELETE")]


def tiles() -> list:
    return [r for r in writes() if r["path"].startswith("/live-activity/")]


def starts() -> list:
    return [r for r in tiles() if r["query"].get("new")]


def updates() -> list:
    return [r for r in tiles() if r["method"] == "POST" and not r["query"].get("new")]


def ends() -> list:
    return [r for r in tiles() if r["method"] == "DELETE"]


def alerts() -> list:
    return [r for r in writes() if r["path"].startswith("/notify-json/")]


def deliver(coordinator, loop, clock, payload: dict) -> None:
    """Run one status response through the real parser and the coordinator.

    `received_at` is the fake clock rather than the wall clock, because the
    coordinator's freshness gate compares the two: a reading stamped with the
    real "now" is months adrift of the fake one and is dropped as stale before
    it can do anything at all.

    `expected_id` is passed for the same reason the poll loop passes it: the
    printer's identity comes from the request that was made, never from the
    body that came back.
    """
    event = status_to_event(payload, received_at=clock.moment(),
                            expected_id=payload["id"])
    assert event is not None, f"nothing parsed from {payload}"
    loop.run_until_complete(coordinator.handle(event))


def test_one_whole_print_from_a_bambuddy_status_response_to_https(gateway, clock):
    """A print picked up mid-flight, paused, resumed and finished, through
    every real layer.

    Two of the steps here are the ones worth wiring end to end. The countdown
    is a minutes-to-seconds conversion that no unit test can prove reaches the
    wire intact, and the ending is now INFERRED from repeated inactive
    readings rather than announced by an event, so the whole terminal path
    depends on a timer that only fires when the pieces agree about what time
    it is.
    """
    REQUESTS.clear()
    Gateway.counter[0] = 0

    config = make_config(
        progress_min_interval_s=10,
        terminal_hold_s=0,
        pause_suppress_s=0,
        running_grace_s=30,
    )
    store = Store(":memory:")
    client = NotifyClient(gateway, config.device_id, config.device_token, timeout=5.0)
    coordinator = Coordinator(config, store, client, alerts=Alerts(config, client))
    loop = asyncio.new_event_loop()

    # --- One reading is not a print ---------------------------------------
    # The only creation path left is "this printer has been RUNNING for a
    # while", so the grace period is the entire defence against a single
    # flapping poll spawning a tile.
    deliver(coordinator, loop, clock, api_status())
    assert not tiles(), "a single RUNNING reading is not enough to start a tile"

    # --- The print gets a tile --------------------------------------------
    clock.advance(45)
    deliver(coordinator, loop, clock, api_status())

    assert len(starts()) == 1, "exactly one tile is created"
    start = starts()[0]
    assert start["path"] == f"/live-activity/{config.device_id}"
    assert start["query"]["token"] == [config.device_token]
    assert start["body"]["title"] == "Workshop H2C"
    assert start["body"]["body"] == "Articulated Octopus"
    assert start["body"]["endsIn"] == 43 * 60, "minutes must be converted to seconds"
    # The reading says 52%, and the tile says 0. BambuBuddy never resets its
    # progress field when a print begins, so a percentage that has not been
    # seen to MOVE may well belong to the previous print. Showing it would put
    # a fresh tile at 52% and then jump it backwards.
    assert start["body"]["progress"] == 0, \
        "a percentage that has not moved yet cannot be believed"
    assert start["body"]["status"] == "Printing"

    # --- It reports progress ----------------------------------------------
    clock.advance(15)
    deliver(coordinator, loop, clock, api_status(progress=58.0, remaining_time=37))

    assert updates(), "the progress reading produced an update"
    latest = updates()[-1]["body"]
    assert latest["endsIn"] == 37 * 60, "minutes must be converted to seconds"
    assert latest["progress"] == 58, "a percentage that has moved is now believed"
    assert latest["status"] == "Printing"
    # The API carries no layer numbers, so there is nothing to put in the
    # trailing slot, and a countdown would outrank it even if there were.
    assert latest["trailing"] is None

    # --- It pauses ---------------------------------------------------------
    clock.advance(2)
    deliver(coordinator, loop, clock,
            api_status(state="PAUSE", progress=58.0, remaining_time=37))

    paused = [r for r in tiles() if r["method"] == "POST" and
              r["body"].get("status") == "Paused"]
    assert paused, "a pause reaches the tile immediately, not after the pacing floor"
    assert paused[-1]["body"]["endsIn"] is None, "a paused print has no countdown"
    assert paused[-1]["body"]["trailing"] is None, \
        "nothing may outrank the word Paused in the trailing slot"
    # A pause is just `state == "PAUSE"`. The status route carries no error
    # codes, so a filament runout and a deliberate pause are indistinguishable
    # and the tile must not guess at anything more specific than "Paused".
    assert all(r["body"].get("status") != "Filament out" for r in tiles())

    # --- It resumes --------------------------------------------------------
    clock.advance(2)
    deliver(coordinator, loop, clock, api_status(progress=71.0, remaining_time=15))

    resumed = [r for r in tiles() if r["method"] == "POST" and
               r["body"].get("status") == "Printing"]
    assert resumed[-1]["body"]["endsIn"] == 15 * 60, "the countdown comes back on resume"
    assert resumed[-1]["body"]["progress"] == 71

    # --- The printer reports FINISH, once ----------------------------------
    # `current_print` is cleared along with the print. The job name has to
    # survive that: it is still owed to the final tile and to the alert, and
    # an empty response field must never blank a name already on screen.
    clock.advance(5)
    finished = api_status(state="FINISH", current_print=None, progress=100.0,
                          remaining_time=0)
    deliver(coordinator, loop, clock, finished)

    assert not ends(), \
        "one inactive reading is not an ending; the API cannot tell a finished " \
        "print from a printer that has simply gone quiet for a moment"
    assert updates()[-1]["body"]["status"] == "Printing", \
        "the tile does not claim an outcome it has not established yet"

    # --- Still FINISH a minute and a half later, so it really is over ------
    # INACTIVE_GRACE_SECONDS. With no terminal event to receive, the only
    # evidence of an ending available is an inactive state that persists.
    clock.advance(95)
    deliver(coordinator, loop, clock, finished)

    done = [r for r in tiles() if r["method"] == "POST" and
            r["body"].get("status") == "Done"]
    assert done, "a sustained FINISH is inferred as a completed print"
    assert done[-1]["body"]["progress"] == 100
    assert done[-1]["body"]["endsIn"] is None, "a finished print has no countdown"
    assert done[-1]["body"]["body"] == "Articulated Octopus", \
        "the job name outlives the response field it came from"

    # --- And the tile ends -------------------------------------------------
    loop.run_until_complete(coordinator.tick())

    assert len(ends()) == 1, "the tile is ended exactly once"
    assert ends()[0]["body"]["progress"] == 100
    assert ends()[0]["body"]["status"] == "Done"
    assert ends()[0]["body"]["keepFor"] == 300
    assert len(starts()) == 1, "no second tile was ever started for this print"

    finished_alert = [r for r in alerts() if r["body"]["title"] == "Print finished"]
    assert len(finished_alert) == 1
    assert "Articulated Octopus" in finished_alert[0]["body"]["text"]
    assert "Workshop H2C" in finished_alert[0]["body"]["text"]
    assert finished_alert[0]["body"].get("timeSensitive") is None, \
        "good news does not break through Focus"

    # --- Nothing leaked ---------------------------------------------------
    # The token belongs in the query string and nowhere else. If it ever turns
    # up inside a body, something is building URLs by hand.
    for request in REQUESTS:
        assert config.device_token not in json.dumps(request["body"] or {})


def test_a_second_printer_never_touches_the_first_ones_tile(gateway, clock):
    """Two printers, two tiles, addressed by their own activity ids.

    The failure this rules out is the one that makes multi-printer support
    subtly wrong: addressing the DEVICE rather than the activity means an
    update for printer B silently rewrites printer A's tile whenever exactly
    one is live.

    The second half guards the other half of the same property. This transport
    has no printer serial, so sessions are keyed on BambuBuddy's database id
    instead. Keying them on the printer's NAME would look identical until
    someone renamed a printer, at which point its running tile would be
    orphaned and a second one started beside it.
    """
    REQUESTS.clear()
    Gateway.counter[0] = 0

    config = make_config(running_grace_s=0, progress_min_interval_s=0)
    store = Store(":memory:")
    client = NotifyClient(gateway, config.device_id, config.device_token, timeout=5.0)
    coordinator = Coordinator(config, store, client, alerts=None)
    loop = asyncio.new_event_loop()

    deliver(coordinator, loop, clock, api_status())
    deliver(coordinator, loop, clock,
            api_status(id=2, name="Bench A1", current_print="Second job",
                       progress=12.0, remaining_time=180))

    assert len(starts()) == 2
    tile_of = {r["body"]["title"]: r["response"]["activityId"] for r in starts()}
    assert len(set(tile_of.values())) == 2, "two prints, two distinct tiles"
    first, second = tile_of["Workshop H2C"], tile_of["Bench A1"]

    # Every update must address an activityId, never the device id.
    clock.advance(5)
    deliver(coordinator, loop, clock,
            api_status(id=2, name="Bench A1", current_print="Second job",
                       progress=30.0, remaining_time=150))

    assert updates(), "the second printer produced an update"
    for request in updates():
        assert not request["path"].endswith(config.device_id), \
            "updates must never be addressed to the device id"
        assert request["path"] == f"/live-activity/{second}", \
            "the second printer's reading may only ever reach the second printer's tile"
    assert all(first not in r["path"] for r in updates()), \
        "the first printer's tile was not touched by the second printer"

    # --- The printer is renamed in BambuBuddy ------------------------------
    clock.advance(5)
    deliver(coordinator, loop, clock,
            api_status(id=2, name="Bench A1 (workshop)", current_print="Second job",
                       progress=35.0, remaining_time=140))

    assert len(starts()) == 2, "a rename is not a new printer and does not start a tile"
    assert updates()[-1]["path"] == f"/live-activity/{second}"
    assert updates()[-1]["body"]["title"] == "Bench A1 (workshop)", \
        "the tile follows the rename rather than being orphaned by it"
