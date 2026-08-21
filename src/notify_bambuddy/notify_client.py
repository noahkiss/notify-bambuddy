# ---------------------------------------------------------------------------
# The Notify! Live Activity API client.
#
# One path, `/live-activity/{id}`, on three verbs, where `{id}` is either a
# DEVICE id (the upsert address) or an ACTIVITY id (the precise address):
#
#   POST   /live-activity/{deviceId}?token=...&new=1   start a new tile
#   POST   /live-activity/{activityId}?token=...       update that tile
#   DELETE /live-activity/{activityId}?token=...       end that tile
#   GET    /live-activity/{activityId}?token=...       status of that tile
#   GET    /live-activity/{deviceId}?token=...         list the device's tiles
#
# Two things about this API shape drive the code below.
#
# FIRST: a GET carrying content parameters ACTS instead of reporting. Only a
# bare GET is a query. So the status and list calls send nothing but `token`,
# and there is no convenience parameter on them that could ever creep in.
#
# SECOND: the HTTP status code alone is not enough to decide what to do. A 410
# can mean the user swiped the tile away (stop, permanently) or that the
# gateway's own housekeeping reaped a tile whose print is still running
# (recreate immediately). Conflating those two is the difference between a
# well-behaved integration and one that permanently blacklists a working
# printer the first time an ETA overruns. So every error becomes a typed
# exception carrying the machine-readable detail the caller needs, and the
# caller is expected to branch on the type, never on the number.
#
# Transport is `urllib.request` from the standard library, called on a worker
# thread via `asyncio.to_thread`-style dispatch. It is a handful of small JSON
# requests per print; a dedicated HTTP client library would be a dependency
# and a supply-chain surface bought for nothing.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import USER_AGENT
from .redact import redact_url

log = logging.getLogger(__name__)

#: The avatar shown beside the title on every push notification this service
#: sends. Notify renders `iconUrl` as a small circular image next to the
#: title, which is what makes an alert identifiable at a glance in a crowded
#: Notification Centre.
#:
#: Hardcoded on purpose. It is branding rather than configuration, it has to
#: be reachable over HTTPS by the phone rather than by this process, and a
#: per-install override would only be a way to end up with a broken image.
#: Change this one line to change it everywhere.
#:
#: It applies to notifications only. The Lock Screen TILE is drawn from an SF
#: Symbol instead, which is `NOTIFY_SYMBOL`, because a Live Activity renders a
#: glyph rather than a fetched image.
NOTIFY_ICON_URL = "https://icons.getnotifyapp.com/icons/mt39aefs-tjpz3wae.png"


# ---------------------------------------------------------------------------
# Errors
#
# The class hierarchy encodes the only question a caller actually has: may I
# try this again, and if so when?
# ---------------------------------------------------------------------------


class NotifyError(Exception):
    """Base for every failure talking to the Notify gateway."""

    #: Whether retrying the identical request could plausibly succeed later.
    retryable = False

    def __init__(self, message: str, status: Optional[int] = None,
                 body: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


class NotifyTransportError(NotifyError):
    """The request never got an HTTP answer: DNS, TCP, TLS, or a timeout.

    Retryable, but with one large caveat that belongs to the caller: a timeout
    on a START is AMBIGUOUS. The gateway may well have created the tile and
    lost only the response. Blindly retrying a start after a timeout is how a
    user ends up with two tiles for one print. The recovery path is to list
    the device's live activities and reconcile, which is why `list_device`
    exists.
    """

    retryable = True


class NotifyValidationError(NotifyError):
    """400. The request itself is wrong and will stay wrong.

    Except for one case, which is why `is_tile_cap` exists: the gateway also
    returns 400 when the device already holds its maximum of 5 live tiles.
    That is a TRANSIENT condition dressed as a permanent one. A caller that
    treats every 400 as fatal will permanently give up on the sixth printer in
    a farm even though a slot frees up twenty minutes later.
    """

    @property
    def is_tile_cap(self) -> bool:
        message = str(self).lower()
        return "maximum" in message and "live activit" in message

    @property
    def retryable(self) -> bool:  # type: ignore[override]
        return self.is_tile_cap


class NotifyAuthError(NotifyError):
    """403. Bad token, or an id the gateway does not know.

    The gateway answers both cases identically and on purpose, so it never
    confirms whether an id exists. That means this error cannot distinguish
    "your token is wrong" from "this activity id is gone from our database",
    and the caller must not guess: for a start it means the credentials are
    wrong, for an update it usually means the activity row was cleaned up.
    """


class NotifyDeviceNotReady(NotifyError):
    """409 on a start. The device cannot show a tile and the gateway knows why.

    Two causes, both fixable only by the human holding the phone: the app has
    never been opened since installing a Live Activity capable build (so no
    push-to-start credential is registered), or Live Activities are switched
    off in Settings. The message says which, and it is worth surfacing
    verbatim rather than paraphrasing.
    """


class NotifyAmbiguous(NotifyError):
    """409 on a DEVICE-addressed call while several tiles are live.

    Carries `activity_ids`. This service always starts with `new=1` and
    always addresses updates by activity id, so it should never provoke this;
    seeing it means another tool is driving the same device, which is fine and
    worth logging rather than treating as an error.
    """

    @property
    def activity_ids(self) -> tuple[str, ...]:
        return tuple(self.body.get("activityIds") or ())


class NotifyGone(NotifyError):
    """410. The tile no longer exists.

    `end_reason` is what matters, and it is NOT in this response: the 410 body
    carries prose. The reliable way to learn the reason is to follow up with
    `GET /live-activity/{activityId}`, which returns a clean `endReason`
    field. `NotifyClient.classify_gone` does exactly that.

    The reasons and what they mean for a caller:

      dismissed      the user swiped the tile away. Final. Do not recreate.
      dead-token     the tile vanished from the device. Treat as dismissed.
      script         this service ended it. Expected; nothing to do.
      never-started  the push never produced a tile on the phone.
      overdue        the countdown lapsed 30 minutes ago with no end call.
      abandoned      a progress-only tile went silent for 2 hours.
      expired        the tile passed Apple's 8-hour ceiling.

    The last four are the gateway reaping a tile for a print that may still be
    running. The correct response to those is to recreate the tile, not to
    give up on the printer.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 body: Optional[dict] = None, end_reason: Optional[str] = None) -> None:
        super().__init__(message, status, body)
        self.end_reason = end_reason

    #: End reasons that mean the USER does not want this tile. Honour them.
    #: Every other reason (`never-started`, `overdue`, `abandoned`, `expired`,
    #: and anything the gateway adds later) means the tile was reaped out from
    #: under a print that may still be running, and is handled by recreating.
    USER_DISMISSED = frozenset({"dismissed", "dead-token"})

    @property
    def user_dismissed(self) -> bool:
        # When the reason could not be established, err toward NOT suppressing
        # future tiles. A missing tile the user did want is a bug they will
        # report; a suppressed printer looks like the integration is broken.
        return (self.end_reason or "") in self.USER_DISMISSED


class NotifyThrottled(NotifyError):
    """429. Apple is silently discarding push-to-start for this device.

    This is the gateway protecting the device's spawn budget after it noticed
    that starts are not producing tiles. It escalates: 30 minutes after 2
    confirmed losses, 3 hours after 4, 6 hours after 6.

    Critically, WAITING ALONE DOES NOT FIX IT. The counter resets when the
    phone reports a fresh push-to-start credential, which happens when the
    user opens the Notify app. So the correct handling is: stop starting
    tiles, honour `retry_after`, and tell the user to open the app once.
    Updates and ends are never gated by this.
    """

    retryable = True

    @property
    def retry_after(self) -> int:
        value = self.body.get("retryAfterSeconds")
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            # A sane default when the body did not say. The shortest real
            # quiet period the gateway imposes is 30 minutes.
            return 1800


class NotifyUpstreamError(NotifyError):
    """502 on a start. Apple could not be reached and NOTHING was created.

    Unambiguously safe to retry: the gateway deletes its provisional row
    before answering, so there is no orphan tile to reconcile against.
    """

    retryable = True


class NotifyDisabled(NotifyError):
    """503. Live Activities are switched off server-side by a kill switch.

    Applies to starts and updates only. Ending a tile and reading its status
    both keep working, which is deliberate: winding down cleanly is exactly
    the operation that must not be blocked. Transient; keep the session,
    back off, and do not tear anything down.
    """

    retryable = True


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartedActivity:
    """The result of a successful start."""

    activity_id: str
    #: Apple's 8-hour ceiling for this specific tile, as an ISO-8601 string.
    #: Parsed by the caller; kept as text here so this module never has to
    #: guess at a timezone.
    expires_at: Optional[str]


@dataclass(frozen=True)
class ActivityStatus:
    """The result of `GET /live-activity/{activityId}`."""

    activity_id: str
    #: One of `starting`, `active`, `dismissed`, `ended`.
    state: str
    #: Populated only for a terminal state. See NotifyGone for the values.
    end_reason: Optional[str]
    expires_at: Optional[str]
    raw: dict

    @property
    def is_live(self) -> bool:
        return self.state in ("starting", "active")

    @property
    def is_confirmed(self) -> bool:
        """True once the tile is known to have appeared on the phone.

        `starting` means the push was accepted but the device has not yet
        reported back. A tile still `starting` a minute and a half after the
        start is very probably never going to appear.
        """
        return self.state == "active"


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class NotifyClient:
    """Async wrapper over the Live Activity endpoints.

    Every public method is a coroutine that runs one blocking HTTP request on
    a worker thread. There is no connection pooling and no keep-alive, which
    is fine at this traffic level: a busy five-printer farm makes on the order
    of one request every three seconds.
    """

    #: In dry-run mode, WRITES are suppressed and reads are not. Starting,
    #: updating and ending a tile never leave the process; `/link`, status and
    #: list still go out, because validating the credentials and reconciling
    #: existing tiles are exactly the things worth exercising in a dry run,
    #: and none of them changes anything on the device.
    _DRY_RUN_SUPPRESSES = ("POST", "DELETE")

    def __init__(self, base_url: str, device_id: str, device_token: str,
                 timeout: float = 15.0, dry_run: bool = False) -> None:
        self._base = base_url.rstrip("/")
        self._device_id = device_id
        self._token = device_token
        self._timeout = timeout
        self._dry_run = dry_run
        # Dry-run bookkeeping: what has been "started" and not yet "ended".
        self._dry_counter = 0
        self._dry_live: dict[str, dict] = {}
        # An explicit default context so certificate verification is on and
        # visibly so, rather than inherited and assumed.
        self._ssl_context = ssl.create_default_context()

    # --- Public API --------------------------------------------------------

    async def link(self) -> dict:
        """Validate the credentials and report what they point at.

        Used once at startup. The response's `type` field is the only way to
        tell a device credential from a group credential BEFORE trying to use
        it, and a group credential must be rejected with a message that says
        so: Live Activities are per-device, and the Live Activity endpoints
        would otherwise answer a group id with the uniform 403 that says only
        "Invalid token or id not found".
        """
        url = f"{self._base}/link?" + urllib.parse.urlencode(
            {"id": self._device_id, "token": self._token}
        )
        try:
            body = await self._request("GET", url, None)
        except NotifyAuthError:
            raise
        except NotifyError as exc:
            # `/link` answers a wrong id or a wrong token with 404, not 403:
            #
            #   {"success": false, "error": "Invalid credentials",
            #    "code": "NOT_FOUND",
            #    "message": "No device or group found with the provided ID and
            #                token combination"}
            #
            # Left unclassified that lands in the generic branch, which the
            # entry point treats as "the gateway is having a moment, carry on".
            # The service then starts happily on a typo'd credential and does
            # nothing at all, forever, which is precisely the failure this
            # startup check exists to prevent. 404 on THIS endpoint is
            # unambiguous, because the route always exists.
            if exc.status == 404:
                raise NotifyAuthError(str(exc), exc.status, exc.body) from None
            raise

        # Belt and braces: the same failure delivered as a 200 with a false
        # `success` flag would otherwise be read as a valid credential whose
        # `type` is missing, and reported as the wrong problem.
        if isinstance(body, dict) and body.get("success") is False:
            raise NotifyAuthError(
                body.get("message") or body.get("error") or "Invalid credentials",
                200, body,
            )
        return body

    async def start(self, content: dict) -> StartedActivity:
        """Start a NEW tile and return its activity id.

        Always `new=1`. The alternative, letting the device address upsert,
        would silently UPDATE another printer's tile whenever exactly one is
        live, and return a 409 whenever two or more are. With several printers
        that is not an edge case, it is the normal state of affairs.

        The cost of `new=1` is that it also skips the gateway's own
        sticky-dismissal protection, so this service has to implement dismissal
        suppression itself. The coordinator does; see the dismissal handling
        there.
        """
        url = self._activity_url(self._device_id, {"new": "1"})
        body = await self._request("POST", url, content)
        activity_id = body.get("activityId")
        if not activity_id:
            raise NotifyError("Start succeeded but returned no activityId", body=body)
        return StartedActivity(activity_id=activity_id, expires_at=body.get("expiresAt"))

    async def update(self, activity_id: str, content: dict) -> None:
        """Partially update a tile.

        Send only what changed. An absent field is left alone; an explicit
        JSON `null` clears it.

        This service DOES clear `endsIn`, on every push where a countdown
        would be a lie: paused, silent, ETA not known yet, and finished. That
        is a deliberate trade. Clearing it turns the tile into a progress-only
        tile, which the gateway reaps after two hours without an update, and
        the unconditional keepalive in coordinator.py is what makes it safe.
        The payoff is that the status word and the layer counter become
        visible at all, since a live countdown outranks both.
        """
        await self._request("POST", self._activity_url(activity_id), content)

    async def end(self, activity_id: str, content: Optional[dict] = None,
                  keep_for: int = 0) -> None:
        """End a tile, optionally leaving final content on screen for a while.

        Idempotent: ending an already-ended tile succeeds. That matters,
        because it means a crash between "decided to end" and "recorded that
        we ended" costs nothing on the retry.
        """
        payload = dict(content or {})
        payload["keepFor"] = keep_for
        await self._request("DELETE", self._activity_url(activity_id), payload)

    async def status(self, activity_id: str) -> ActivityStatus:
        """Read one tile's state and, if it has ended, why.

        Sends nothing but the token: a GET carrying content parameters would
        ACT rather than report.
        """
        body = await self._request("GET", self._activity_url(activity_id), None)
        return ActivityStatus(
            activity_id=body.get("activityId") or activity_id,
            state=body.get("state") or "unknown",
            end_reason=body.get("endReason"),
            expires_at=body.get("expiresAt"),
            raw=body,
        )

    async def list_device(self) -> tuple[ActivityStatus, ...]:
        """List every LIVE tile on the device.

        The crash-recovery path, and the answer to an ambiguous start: after a
        timeout on a start, this says whether the tile was created after all,
        so the retry can adopt it instead of creating a duplicate.
        """
        body = await self._request("GET", self._activity_url(self._device_id), None)
        activities = body.get("activities") or []
        return tuple(
            ActivityStatus(
                activity_id=item.get("activityId", ""),
                state=item.get("state") or "unknown",
                end_reason=item.get("endReason"),
                expires_at=item.get("expiresAt"),
                raw=item,
            )
            for item in activities
            if item.get("activityId")
        )

    async def notify(self, text: str, title: str, group_type: str,
                     time_sensitive: bool = False) -> None:
        """Send an ordinary push notification, separate from any tile.

        Live Activity traffic is deliberately NOT recorded in Notify's history:
        a printer pushing percent every few minutes would bury everything else
        the user actually wants to look back at. That is the right call for
        progress, and the wrong one for the handful of moments that matter, so
        those get a real notification as well as a tile change. A finished
        print then leaves something behind after the tile disappears, and a
        filament runout at 3am can break through Focus.

        `group_type` becomes an iOS thread id, so every alert about the same
        printer stacks into one thread instead of scattering down the
        notification list.

        Every notification carries `NOTIFY_ICON_URL` as its avatar, so alerts
        from this service are recognisable at a glance rather than looking
        like a generic Notify push.

        `POST /notify-json` is used rather than the URL form because the body
        carries the text as-is, with no URL encoding to inflate newlines, and
        because it is the endpoint the API documents as preferred.
        """
        payload = {
            "text": text,
            "title": title,
            "groupType": group_type,
            "iconUrl": NOTIFY_ICON_URL,
        }
        if time_sensitive:
            # Raises the interruption level so the alert survives Do Not
            # Disturb and Focus. A request, not a guarantee: the user's own
            # Focus settings have the final say, and iOS silently downgrades
            # rather than failing when they say no.
            payload["timeSensitive"] = True
        url = f"{self._base}/notify-json/{urllib.parse.quote(self._device_id)}?" + \
            urllib.parse.urlencode({"token": self._token})
        await self._request("POST", url, payload)

    async def classify_gone(self, activity_id: str, error: NotifyGone) -> NotifyGone:
        """Fill in a 410's `end_reason` by asking the gateway why.

        The 410 body carries prose, not a machine-readable reason, but the
        status endpoint returns `endReason` as a clean field and stays
        readable for a day after a tile ends. This turns "410" into the
        actionable distinction between "the user does not want this" and "the
        gateway tidied up a tile whose print is still running".

        Best effort: if the follow-up itself fails, the original error comes
        back unchanged with no reason, and the caller's fallback (do not
        suppress) applies.
        """
        if error.end_reason:
            return error
        try:
            state = await self.status(activity_id)
        except NotifyError as exc:
            log.debug("Could not classify 410 for %s: %s", activity_id, exc)
            return error
        if state.end_reason:
            return NotifyGone(str(error), error.status, error.body, end_reason=state.end_reason)
        if state.state == "dismissed":
            return NotifyGone(str(error), error.status, error.body, end_reason="dismissed")
        return error

    # --- Internals ---------------------------------------------------------

    def _activity_url(self, ident: str, extra: Optional[dict] = None) -> str:
        """Build a request URL. The token always travels as a query parameter.

        That is the API's design, not a choice made here: the same address has
        to work when pasted into a webhook field. It does mean every URL this
        module builds is a live credential, which is why nothing here logs a
        URL without passing it through `redact_url` first.
        """
        params = {"token": self._token}
        if extra:
            params.update(extra)
        return f"{self._base}/live-activity/{urllib.parse.quote(ident)}?" + urllib.parse.urlencode(params)

    async def _request(self, method: str, url: str, payload: Optional[dict]) -> dict:
        if self._dry_run:
            synthetic = self._dry_run_response(method, url, payload)
            if synthetic is not None:
                return synthetic
        loop = asyncio.get_running_loop()
        # `run_in_executor` rather than `asyncio.to_thread`: the executor form
        # works identically on every supported Python and makes the thread
        # pool an explicit, inspectable thing rather than an implicit one.
        return await loop.run_in_executor(None, self._request_blocking, method, url, payload)

    #: Prefix of every synthetic activity id handed out in a dry run. Reads
    #: of these ids are answered locally too, so a dry run never asks the real
    #: gateway about a tile that only exists in this process.
    _DRY_PREFIX = "LADRY"

    def _dry_run_response(self, method: str, url: str, payload: Optional[dict]) -> Optional[dict]:
        """Answer a request locally in dry-run mode, or return None to send it.

        Writes (start, update, end, notify) are logged and answered with a
        plausible success. Reads of a synthetic tile are answered from the
        local record of what was "started", so the verification poll and the
        reconciliation list see a consistent world instead of hitting the real
        gateway with ids it has never heard of. `/link` and reads of REAL ids
        still go out, because validating the credentials and noticing a real
        leftover tile are the parts of a dry run worth keeping honest.
        """
        path = urllib.parse.urlparse(url).path
        ident = path.rsplit("/", 1)[-1]

        if method in self._DRY_RUN_SUPPRESSES:
            log.info("[dry-run] %s %s payload=%s", method, redact_url(url), payload)
            if path.startswith("/live-activity/") and ident == self._device_id:
                self._dry_counter += 1
                activity_id = f"{self._DRY_PREFIX}{self._dry_counter:03d}"
                self._dry_live[activity_id] = dict(payload or {})
                return {"success": True, "activityId": activity_id,
                        "expiresAt": None}
            if method == "DELETE":
                self._dry_live.pop(ident, None)
            return {"success": True, "activityId": ident}

        if method == "GET" and path.startswith("/live-activity/"):
            if ident == self._device_id:
                return {"activities": [
                    {"activityId": aid, "state": "active", "endReason": None,
                     "content": content, "expiresAt": None}
                    for aid, content in self._dry_live.items()
                ]}
            if ident.startswith(self._DRY_PREFIX):
                if ident in self._dry_live:
                    return {"activityId": ident, "state": "active", "endReason": None,
                            "content": self._dry_live[ident], "expiresAt": None}
                return {"activityId": ident, "state": "ended", "endReason": "script"}
        return None

    def _request_blocking(self, method: str, url: str, payload: Optional[dict]) -> dict:
        data = None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            # Required on every JSON endpoint. Omitting it makes the gateway
            # fall back to the query-string dialect and ignore the body.
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout,
                                        context=self._ssl_context) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._to_error(exc)
        except urllib.error.URLError as exc:
            raise NotifyTransportError(f"Could not reach the Notify gateway: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            raise NotifyTransportError(f"Notify request failed: {exc}")
        except http.client.HTTPException as exc:
            # BadStatusLine, IncompleteRead, RemoteDisconnected and friends.
            # These are NOT OSError subclasses, so without this they would
            # escape past every `except NotifyError` in the coordinator and
            # surface as an unhandled exception. A truncated response from a
            # proxy is exactly as transient as a timeout, and gets the same
            # ambiguity caveat: on a START, the tile may exist anyway.
            raise NotifyTransportError(f"Notify sent a malformed response: {exc}")

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NotifyError(f"Notify returned a response that is not JSON: {exc}")
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    @staticmethod
    def _to_error(exc: urllib.error.HTTPError) -> NotifyError:
        """Turn an HTTP error response into the right typed exception."""
        status = exc.code
        body: dict = {}
        try:
            raw = exc.read()
            if raw:
                decoded = json.loads(raw.decode("utf-8"))
                if isinstance(decoded, dict):
                    body = decoded
        except Exception:  # noqa: BLE001 - a malformed error body must not mask the error
            body = {}

        message = body.get("message") or body.get("error") or exc.reason or f"HTTP {status}"

        if status == 400:
            return NotifyValidationError(message, status, body)
        if status == 403:
            return NotifyAuthError(message, status, body)
        if status == 409:
            # Two different 409s share the code. The ambiguity one is the only
            # one that carries an activityIds array.
            if body.get("activityIds"):
                return NotifyAmbiguous(message, status, body)
            return NotifyDeviceNotReady(message, status, body)
        if status == 410:
            return NotifyGone(message, status, body)
        if status == 429:
            return NotifyThrottled(message, status, body)
        if status == 502:
            return NotifyUpstreamError(message, status, body)
        if status == 503:
            return NotifyDisabled(message, status, body)
        # Any other 5xx is worth retrying; any other 4xx is not.
        error = NotifyError(message, status, body)
        if status >= 500:
            error.retryable = True  # type: ignore[misc]
        return error
