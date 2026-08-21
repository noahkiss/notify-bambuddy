# ---------------------------------------------------------------------------
# A private health endpoint.
#
# Two questions it answers, both of which are otherwise unanswerable from
# outside:
#
#   1. IS THIS SERVICE ALIVE? The ordinary container liveness question.
#
#   2. IS IT ACTUALLY SEEING BAMBUBUDDY? This one matters more than it sounds.
#      A rejected API key, a BambuBuddy that has moved, and a farm where
#      simply nothing is printing all look identical from the outside: no
#      tiles appear, and no error is obvious anywhere. Reporting when the last
#      poll succeeded, alongside the last error, makes the three tellable
#      apart in one look.
#
# Bound to 127.0.0.1 by default. Nothing here is a credential, but it does
# list printer names, job names and the BambuBuddy address, so it is not
# something to expose to a network by accident. It deliberately contains no
# token and no key, because the whole point of a diagnostics endpoint is that
# people paste its output into issue reports.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    """Serves the snapshot. Two paths, no routing framework required."""

    #: Injected by the server factory below.
    snapshot: Callable[[], dict]

    #: Socket timeout for one request. Without it a client that opens a
    #: connection and sends nothing holds the handler forever, and a
    #: single-threaded server would stop answering every other probe while a
    #: threaded one would leak a thread per idle connection.
    timeout = 10

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ("/", "/healthz"):
            # Liveness only: the process is up and the loop is turning. It
            # deliberately does NOT depend on BambuBuddy being reachable,
            # because restarting this service does not fix that outage and
            # a restart loop during one would only make recovery slower.
            self._respond(200, {"status": "ok"})
            return

        if path == "/readyz":
            # Readiness: actually able to see printer state. That means the
            # last poll succeeded AND at least one ever has, because a poll
            # that fails is either a rejected key or an unreachable
            # BambuBuddy, and this service can do no useful work through
            # either.
            #
            # Legitimately false for a moment at startup, which is what a
            # probe's start period is for.
            snapshot = self._safe_snapshot()
            source = snapshot.get("source", {})
            ready = (source.get("consecutive_failures") == 0
                     and source.get("polls_succeeded", 0) > 0)
            self._respond(200 if ready else 503, {"ready": ready, **snapshot})
            return

        if path == "/status":
            self._respond(200, self._safe_snapshot())
            return

        self._respond(404, {"error": "not found"})

    def _safe_snapshot(self) -> dict:
        try:
            return type(self).snapshot()
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            log.exception("Health snapshot failed")
            return {"error": str(exc)}

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # No caching: every field here is a point-in-time measurement.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler writes to stderr by default, which would put
        # a line in the logs for every liveness probe. Route it to debug.
        log.debug("health: " + fmt, *args)


class HealthServer:
    """A tiny HTTP server on its own thread."""

    def __init__(self, host: str, port: int, snapshot: Callable[[], dict]) -> None:
        handler = type("BoundHandler", (_Handler,), {"snapshot": staticmethod(snapshot)})
        # Threaded, so one slow or idle client cannot block liveness probes
        # from everyone else, and so `shutdown()` is not stuck behind a
        # request that is still in flight.
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health", daemon=True
        )
        self.host = host
        self.port = port

    def start(self) -> None:
        self._thread.start()
        log.info("Health endpoint listening on http://%s:%d/status", self.host, self.port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
