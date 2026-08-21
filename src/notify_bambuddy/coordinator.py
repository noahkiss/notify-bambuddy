# ---------------------------------------------------------------------------
# The state machine.
#
# One session per printer, because a printer prints one thing at a time. That
# single fact carries most of the design: nothing carries a print id, so the
# only sound way to decide which print just ended is "the open session for
# this printer IS the print that just ended". Matching on filename looks
# reasonable and fails on real hardware, because the name reported as a print
# winds down is not always the name it started under.
#
# The rules that are not obvious, each of which exists because the obvious
# version is wrong:
#
#   THERE IS NO START SIGNAL. Nothing announces that a print began; a printer
#   is simply reported RUNNING one poll and was not the poll before. So
#   "RUNNING with no open session, sustained for a grace window" is THE
#   creation trigger. The grace window exists because a single flapping
#   reading is not a print.
#
#   AN OUTCOME IS NOT FINAL. Nothing announces the end of a print either, so
#   an outcome is INFERRED from the printer reporting an inactive state for a
#   while. That inference is good but not certain: a printer that drops off
#   the network mid-print looks exactly like one that stopped. So a terminal
#   outcome writes terminal CONTENT immediately and starts a hold; the tile is
#   only ended when the hold expires, which gives a printer that comes back
#   time to revoke it. Otherwise a blip at 40% tells the user "Cancelled" for
#   a print that goes on to succeed.
#
#   410 IS NOT ONE THING. It can mean the user swiped the tile away, or that
#   the gateway's housekeeping reaped a tile whose print is still running.
#   Treating them alike is how an integration permanently blacklists a working
#   printer the first time an ETA overruns. Every 410 is classified by asking
#   the gateway for the tile's `endReason` before deciding.
#
#   READINGS CAN ARRIVE OUT OF ORDER. A request delayed in flight can land
#   after a newer one, and applying it would rewind the tile. Every reading is
#   therefore gated on its own age and on the newest one already applied
#   before it is allowed to create or advance anything.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .config import Config
from .content import (
    build_content,
    commit_sent_eta,
    countdown_after,
    job_display_name,
    payload_hash,
    terminal_content,
)
from .models import (
    ACTIVE_STATES,
    INACTIVE_STATES,
    STATE_FAILED,
    STATE_FINISH,
    STATE_PAUSE,
    STATE_RUNNING,
    Delivery,
    Outcome,
    Phase,
    PrinterStatus,
    SessionState,
    parse_timestamp,
)
from .notify_client import (
    ActivityStatus,
    NotifyAuthError,
    NotifyClient,
    NotifyDeviceNotReady,
    NotifyDisabled,
    NotifyError,
    NotifyGone,
    NotifyThrottled,
    NotifyTransportError,
    NotifyUpstreamError,
    NotifyValidationError,
)
from .store import Store

log = logging.getLogger(__name__)

#: How long a printer must keep reporting an inactive state (FINISH/FAILED/
#: IDLE) before its session is closed. Nothing announces the end of a print,
#: so this is how one is recognised. Long enough that a single odd reading
#: between two running ones cannot end a live tile.
INACTIVE_GRACE_SECONDS = 90.0

#: How long to back off all starts after the gateway reports its kill switch
#: is on, or the device cannot show tiles. Ends and updates keep working.
SERVICE_BACKOFF_SECONDS = 300.0

#: How long to stop talking to the gateway after a transport failure (DNS,
#: TCP, TLS, timeout). Short, because outages are usually short, but long
#: enough that five queued printers do not each cost a 15 second timeout on
#: the event loop every five second tick. Terminal sends ignore it.
GATEWAY_BACKOFF_SECONDS = 60.0

#: When a started tile has still not appeared on the phone, how long to stop
#: starting more. Escalates per consecutive loss, mirroring the gateway's own
#: ladder, because the usual cause (a stale start credential after an app
#: update) is fixed only by the user opening the app, and every start sent
#: before that is a push Apple discards while keeping its throttle shut.
UNCONFIRMED_START_BACKOFF = (900.0, 3600.0, 10800.0)



class Coordinator:
    """Owns every session and every call to the Notify gateway."""

    def __init__(self, config: Config, store: Store, client: NotifyClient,
                 alerts=None) -> None:
        self._config = config
        self._store = store
        self._client = client
        self._alerts = alerts

        #: Live sessions, keyed by printer serial. At most one per printer.
        self._sessions: dict[str, SessionState] = {}

        #: The newest status frame per printer, kept so that a terminal event
        #: can be cross-checked against what the printer is currently saying.
        self._latest_status: dict[str, PrinterStatus] = {}

        #: When each printer was first seen RUNNING with no open session.
        #: Creation waits out `running_grace_s` so a single stale or flapping
        #: frame cannot spawn a tile.
        self._running_since: dict[str, float] = {}

        #: When each printer first started reporting an inactive state while a
        #: session was still open. Feeds INACTIVE_GRACE_SECONDS.
        self._inactive_since: dict[str, float] = {}

        #: Sessions whose start is waiting for a tile slot to free up. The
        #: gateway reports the 5-tile cap as a 400, which every sane client
        #: treats as permanent, so this queue is what stops the sixth printer
        #: in a farm from being written off forever.
        self._waiting_for_slot: list[str] = []

        #: When this process started. Used to tell "no telemetry yet" from
        #: "the printer went quiet", which are the same thing to a session
        #: recovered from disk and are not the same decision at all.
        self._started_at: float = time.time()

        #: Until when no new tile is started, for a DEVICE-wide reason: a
        #: 429 throttle, a device that cannot show tiles, rejected
        #: credentials, the kill switch, or a started tile that never became
        #: visible. Ends and updates are never gated by this, because the
        #: gateway does not gate them either. See `_block_starts`.
        self._start_blocked_until: float = 0.0
        self._start_blocked_reason: str = ""

        #: Serial -> the earliest time to retry a start that failed.
        self._retry_start_after: dict[str, float] = {}

        #: Until when the gateway is presumed unreachable. Set on a transport
        #: failure; routine updates and starts wait it out, terminal sends
        #: do not.
        self._gateway_down_until: float = 0.0

        #: Consecutive started tiles that never became visible. Drives the
        #: escalating start backoff; reset when any tile is confirmed.
        self._unconfirmed_starts: int = 0

        #: Tiles whose end could not be delivered, keyed by activity id.
        #: SEPARATE from `_sessions` on purpose: that is keyed by serial and
        #: holds one session per printer, so a supersede or a new print
        #: immediately evicts the session whose tile still needs ending, and
        #: the retry would never see it again. A tile nothing can end is the
        #: worst outcome available (it sits on the Lock Screen until the
        #: gateway reaps it hours later), so it gets its own place to live.
        self._ends_owed: dict[str, SessionState] = {}

        #: Activity ids whose tile has not been confirmed as visible yet, with
        #: the deadline for checking. See `_verify_starts`.
        self._verify_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def recover(self) -> None:
        """Reattach to tiles this service owned before it stopped.

        Four outcomes per stored session, and getting them right is what stops
        a restart from either orphaning a tile on someone's Lock Screen or
        creating a second one next to it:

          still live   -> adopt it and carry on, mid-countdown.
          dismissed    -> record the dismissal so nothing recreates it.
          reaped/gone  -> forget the tile. If the print is genuinely still
                          running, the status topic re-creates one through the
                          normal path within a grace window.
          never got one -> reconcile if a start was in flight when the process
                          stopped, then retire it and wait for live telemetry.
                          What the store remembers is that a print WAS
                          running, which is not evidence about now.
        """
        stored = self._store.load_live_sessions()
        if not stored:
            return

        now = time.time()
        log.info("Recovering %d session(s) from the previous run", len(stored))

        # One list call answers every stored session at once, and is also the
        # reconciliation path for a start whose response was lost.
        live_by_id: dict[str, ActivityStatus] = {}
        try:
            for activity in await self._client.list_device():
                live_by_id[activity.activity_id] = activity
        except NotifyError as exc:
            # Without the list this cannot tell a live tile from a dead one.
            # Keeping the sessions and letting the normal update path discover
            # the truth via 410 is safer than dropping them, which would
            # orphan any tile that IS still live.
            log.warning("Could not list live activities during recovery: %s", exc)
            for session in stored:
                if session.activity_id:
                    # There IS a tile, and it may still be live. Keep the
                    # session; the normal update path discovers the truth via
                    # a 410, which is safer than dropping it and orphaning a
                    # tile that is still up there.
                    self._readopt(session, now)
                    if session.delivery is Delivery.ENDING:
                        self._ends_owed[session.activity_id] = session
                else:
                    # No tile, and no way to reconcile right now. Retiring it
                    # is the only safe move: re-adopting would park a session
                    # with no tile and no path to one, which blocks the
                    # status-topic creation path for that printer entirely.
                    log.info("Printer %s had no tile; waiting for fresh telemetry",
                             session.serial)
                    session.delivery = Delivery.ENDED
                    self._store.save_session(session)
            return

        for session in stored:
            if session.activity_id and session.activity_id in live_by_id:
                self._readopt(session, now)
                log.info(
                    "Reattached to tile %s for printer %s", session.activity_id, session.serial
                )
                continue

            if not session.activity_id and session.is_live():
                # Never got a tile, or died mid-start. Two things have to
                # happen before it can be given one.
                #
                # First, reconcile: a session flagged `start_ambiguous` was in
                # the middle of a start when the process stopped, so the
                # gateway may hold a tile for it. The device list is already
                # in hand, so adopt it rather than creating a second.
                if session.start_ambiguous:
                    self._readopt(session, now)
                    if await self._adopt_orphan(session, now):
                        log.info("Adopted a tile left over from an interrupted start on %s",
                                 session.serial)
                        continue
                    self._sessions.pop(session.serial, None)

                # Second, do NOT simply queue it. What the store remembers is
                # that a print was running when this process stopped, which
                # may have been days ago; the print may be long finished. The
                # session is parked awaiting fresh evidence, and the normal
                # creation path gives it a tile only once a live RUNNING or
                # PAUSE reading confirms the print is real.
                log.info(
                    "Printer %s had no tile; waiting for fresh telemetry before creating one",
                    session.serial,
                )
                session.delivery = Delivery.ENDED
                self._store.save_session(session)
                continue

            if session.activity_id:
                # Not in the live list. Ask why, because the answer decides
                # whether a new tile may be created for this print.
                try:
                    state = await self._client.status(session.activity_id)
                    reason = state.end_reason or ("dismissed" if state.state == "dismissed" else None)
                except NotifyError:
                    reason = None
                if reason in NotifyGone.USER_DISMISSED and session.outcome is None:
                    # Only when the print was still going. A tile swiped away
                    # AFTER its print finished carries no instruction about
                    # the next one, and recording it would silence a printer
                    # for the dismissal window over a print that is already
                    # over.
                    self._store.record_dismissal(session.serial)
                    log.info(
                        "Tile for printer %s was dismissed while we were down; not recreating",
                        session.serial,
                    )
                else:
                    log.info(
                        "Tile for printer %s is gone (%s); a new one will be created if the "
                        "print is still running", session.serial, reason or "unknown reason",
                    )

            session.delivery = Delivery.ENDED
            self._store.save_session(session)

    def _readopt(self, session: SessionState, now: float) -> None:
        """Take a stored session back into memory after a restart.

        The liveness clock has to be reset or the session lies about the
        present: downtime is not silence, and without this a service restarted
        after forty minutes would mark every running print abandoned on its
        first tick.

        Only sessions with a tile still on the phone come through here.
        Tile-less ones are retired in `recover` and rebuilt from live
        telemetry, because the store remembers what WAS running, which is not
        evidence about now.
        """
        session.last_status_ts = now
        self._sessions[session.serial] = session

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def handle(self, event: Any) -> None:
        """Route one reading from the poller.

        One event shape, so this is a type check rather than a dispatch table.
        It stays because the queue is typed loosely and a malformed event must
        be ignored rather than crash the loop.
        """
        if isinstance(event, PrinterStatus):
            await self._on_status(event)

    # --- Status ------------------------------------------------------------

    async def _on_status(self, status: PrinterStatus) -> None:
        serial = status.serial
        now = time.time()
        source_ts = status.timestamp.timestamp()

        # `"unknown"` is BambuBuddy's placeholder, written before the first
        # telemetry frame answers after a reconnect and when it presumes a
        # smart-plug Auto Off. It carries no information at all, so it is a
        # no-op: never a transition, never a creation, never an ending.
        if status.is_unknown:
            return

        # Freshness gate. Satisfied by construction most of the time, because
        # a reading is stamped when it arrives, but it still earns its place:
        # it is the only thing standing between a wrong clock and a tile that
        # never updates again. A live reading is always a round trip old.
        age = now - source_ts
        if age < -self._config.retained_max_age_s:
            # Stamped in the future. Either the publisher's clock is wrong or
            # ours is, and applying it would set `last_source_ts` beyond
            # anything real, silently dropping every subsequent frame until
            # the clock caught up.
            log.warning(
                "Ignoring a reading for %s stamped %.0fs in the FUTURE; check this "
                "machine's clock", serial, -age,
            )
            return
        if age > self._config.retained_max_age_s:
            log.debug(
                "Ignoring a reading for %s: %.0fs old, older than the %ds freshness window",
                serial, age, self._config.retained_max_age_s,
            )
            return

        # `connected: false` means every OTHER field in this payload is a
        # last-seen value rather than a reading. Note the frame's arrival for
        # liveness, but do not let stale numbers advance the tile.
        if not status.connected:
            # NOT a liveness refresh. BambuBuddy keeps publishing these while
            # a printer is unreachable, and counting them as telemetry means
            # the tile never reaches "Connection lost" and never gets
            # abandoned: it just sits there showing the last numbers forever.
            # The frame proves BambuBuddy is alive, not the printer.
            log.debug("Ignoring a disconnected status frame for %s", serial)
            return

        self._latest_status[serial] = status

        session = self._sessions.get(serial)

        if session is None:
            await self._maybe_create_from_status(status, now)
            return

        if session.phase is Phase.TERMINAL:
            # Terminal-sticky, with ONE exception. A terminal marked
            # provisional is a failure BambuBuddy reported while the printer
            # was still running, which is what its restart reconciliation
            # does to every print it finds mid-flight. If the printer then
            # keeps reporting RUNNING with a fresher timestamp than the event,
            # the print did not stop and the tile must come back.
            if session.terminal_provisional and status.state == STATE_RUNNING and \
                    source_ts > (session.terminal_at or 0):
                log.info(
                    "%s is still RUNNING after a provisional %s; taking the tile back",
                    serial, session.outcome.value if session.outcome else "terminal",
                )
                session.outcome = None
                session.terminal_at = None
                session.terminal_provisional = False
                session.alert_sent = False
                session.phase = Phase.PRINTING
                session.delivery = (
                    Delivery.ACTIVE if session.activity_id else Delivery.PENDING
                )
                if not session.activity_id:
                    self._queue_for_slot(session)
            else:
                # A reading stamped before the terminal one can still arrive
                # after it, if a request was slow in flight. Applying it would
                # put a finished print back on screen.
                return

        # Drop, never queue, a frame older than one already applied. Queuing
        # would just rewind the tile a moment later.
        if source_ts < session.last_source_ts:
            return

        # How long since this printer was last heard from, BEFORE this frame
        # is counted. A long gap changes what a layer reset means, so it has
        # to be measured before `last_status_ts` is advanced.
        gap = now - session.last_status_ts if session.last_status_ts > 0 else 0.0

        session.last_source_ts = source_ts
        session.last_status_ts = now

        if await self._detect_new_print(session, status, now, gap):
            return

        self._apply_status_fields(session, status)

        # A dismissed session keeps tracking the print, phase and all. It has
        # no tile to update, but it MUST still advance: the inactive grace is
        # what retires it, and a session parked in `_sessions` blocks the
        # creation path for that printer permanently. That was a real bug: one
        # swipe plus one missed ending silenced a printer for good.
        await self._advance_phase(session, status, now)
        if session.delivery is Delivery.DISMISSED:
            return
        await self._maybe_send(session, now)

    def _open_session(self, serial: str, now: float, *,
                      status: Optional[PrinterStatus] = None,
                      printer_name: Optional[str] = None,
                      job_name: Optional[str] = None) -> SessionState:
        """Create a session for a new print and install it as the printer's one.

        The single place a session is born, from either of the two things that
        can announce a print: a status frame (`status`) or a start event
        (`event`). The two differ in exactly one interesting way, which is how
        much the current progress reading can be believed:

          from a status frame, the reading is a baseline: trust progress once
          it DROPS below it (the new print has started from 0) or once a layer
          number appears.

          from a start event, the reading is known to belong to the PREVIOUS
          print, because the event is proof this one is new and BambuBuddy
          never resets progress at print start. The baseline is therefore set
          to -1.0 so the drop rule can never fire and only a real layer number
          earns trust. A baseline of 100.0 would be satisfied by literally any
          reading, including the last print's 97%, which is the exact number
          the pin exists to keep off the tile.
        """
        assert status is not None, "a session is only ever born from a reading"
        source_ts = status.timestamp.timestamp()
        # Classified from the printer's own state rather than left at STARTING.
        # A tile that appears saying "Starting" and corrects itself to
        # "Printing" a second later costs a second push and tells the user
        # nothing they did not already know.
        phase = Phase.PAUSED if status.state == STATE_PAUSE else Phase.PRINTING
        baseline = status.progress
        name = printer_name or status.printer_name

        session = SessionState(
            session_key=f"{serial}:{int(now)}",
            serial=serial,
            printer_name=name,
            job_name=job_name,
            phase=phase,
            created_at=now,
            last_source_ts=source_ts,
            last_status_ts=now,
            progress_at_detection=baseline,
        )
        self._apply_status_fields(session, status)
        self._sessions[serial] = session
        self._running_since.pop(serial, None)
        self._inactive_since.pop(serial, None)
        return session

    async def _maybe_create_from_status(self, status: PrinterStatus, now: float) -> None:
        """Create a session for a print that is running with no tile.

        This is the creation path for EVERY print, since nothing announces a
        start. It therefore also covers a print already in flight when this
        service came up, and one started from the printer's own touchscreen.
        """
        serial = status.serial

        if status.state not in ACTIVE_STATES:
            self._running_since.pop(serial, None)
            return

        first_seen = self._running_since.setdefault(serial, now)
        if now - first_seen < self._config.running_grace_s:
            return

        if self._store.is_dismissed(serial):
            return

        session = self._open_session(serial, now, status=status)
        log.info(
            "Detected a print already running on %s (%s); creating a tile",
            session.printer_name or serial, status.state,
        )
        await self._start_tile(session, now)

    def _continuously_running(self, session: SessionState, gap: float) -> bool:
        """True when this printer has been heard from recently enough that it
        cannot have finished one print and started another in between.

        A genuinely new print passes through FINISH or IDLE on its way, and
        BambuBuddy publishes status at the end of every telemetry frame, so a
        printer that has been reporting RUNNING without a gap longer than the
        silence threshold has been running the SAME job the whole time.
        """
        return 0 <= gap < self._config.silence_after_s and session.phase is not Phase.SILENT

    async def _detect_new_print(self, session: SessionState, status: PrinterStatus,
                                now: float, gap: float) -> bool:
        """Spot a genuinely new print replacing the one this session tracks.

        The edge is the LAYER NUMBER going backwards while running. BambuBuddy
        resets `layer_num` to 0 on every detected new print. Progress is not
        usable for this: it is never reset at print start, so it carries the
        previous print's percentage into the new one, and it also drops to 0
        on a cancel.

        BUT BambuBuddy resets `layer_num` on a mid-job FILE CHANGE too, which
        is exactly what a multi-plate project does at every plate boundary,
        and those arrive with the printer reporting RUNNING the whole way
        through. A layer reset on a printer that has been continuously
        running is therefore a plate change inside one job, and the tile is
        kept. Only a layer reset after a GAP in telemetry (the printer was
        unheard-of long enough to have finished and restarted) is a new print.

        Returns True when it replaced the session, in which case the caller
        must stop processing this frame against the old one.
        """
        if status.state != STATE_RUNNING:
            return False
        # Only meaningful once the old session had actually got somewhere.
        if session.layer_num <= 1 or status.layer_num >= session.layer_num:
            return False

        if self._continuously_running(session, gap):
            log.info(
                "Layer count on %s went %d -> %d with the printer running throughout: "
                "a plate change within the same job, keeping the tile",
                session.serial, session.layer_num, status.layer_num,
            )
            return False

        log.info(
            "Layer count on %s went %d -> %d after %.0fs of silence: treating this as a "
            "new print", session.serial, session.layer_num, status.layer_num, gap,
        )
        await self._supersede(session, now)

        # A genuinely new print clears any dismissal for this printer. Without
        # this, swiping away one print's tile would also suppress the next
        # print in the one case a session key cannot distinguish: an immediate
        # reprint of the identical file started while this service was down.
        self._store.clear_dismissal(session.serial)

        replacement = self._open_session(session.serial, now, status=status,
                                         printer_name=session.printer_name)
        await self._start_tile(replacement, now)
        return True

    def _apply_status_fields(self, session: SessionState, status: PrinterStatus) -> None:
        """Copy the display fields off a status frame onto a session."""
        if status.printer_name:
            session.printer_name = status.printer_name

        name = job_display_name(status.subtask_name, status.gcode_file)
        if name:
            session.job_name = name

        # Progress cannot be believed until the new print has demonstrably
        # moved. BambuBuddy never resets its progress field at print start, so
        # the first frames of a new print carry the last print's percentage;
        # without this gate a fresh tile appears at 97% and then jumps back.
        if not session.progress_trustworthy:
            # A reading that has MOVED from the one taken when this session
            # was opened, in either direction, is describing THIS print rather
            # than carrying the last one's number.
            #
            # Both directions matter, for different reasons. A DROP is the
            # firmware replacing a finished print's high percentage with the
            # new print's low one, which is exactly the case the gate exists
            # for. A CLIMB is a print this service picked up part way through,
            # where the baseline was already genuine; without accepting a
            # climb the tile would sit at 0% for the whole print, which is a
            # bug this had until it was caught by replaying real telemetry.
            #
            # A stale carry-over cannot climb: it sits at the finished print's
            # percentage, unchanged, until the firmware overwrites it
            # wholesale. So a climb is never the thing being defended against.
            if status.progress != session.progress_at_detection:
                session.progress_trustworthy = True
        session.progress = status.progress

        session.eta_seconds = status.eta_seconds
        # Layer fields only from a frame that describes a print in flight. A
        # FINISH or IDLE frame carries zeros, and copying those in would make
        # the next RUNNING frame look like a layer reset, which is the
        # new-print edge, and would also blank the layer counter on a tile
        # that is about to show its final outcome.
        if status.state in ACTIVE_STATES:
            session.layer_num = status.layer_num
            session.total_layers = status.total_layers

    async def _advance_phase(self, session: SessionState, status: PrinterStatus,
                             now: float) -> None:
        """Move a session between PRINTING, PAUSED and the inactive watch."""
        serial = session.serial

        if status.state == STATE_PAUSE:
            self._inactive_since.pop(serial, None)
            # BambuBuddy's own plate detection can pause a print seconds after
            # it starts, when it sees objects left on the bed. Rendering that
            # momentary pause would make every print's tile flicker through
            # "Paused" on its way to "Printing".
            if now - session.created_at < self._config.pause_suppress_s:
                return
            session.phase = Phase.PAUSED
            return

        if status.state == STATE_RUNNING:
            self._inactive_since.pop(serial, None)
            session.phase = Phase.PRINTING
            return

        if status.state in INACTIVE_STATES:
            # NOT a terminal decision on its own. The status topic is stale
            # for days after a printer powers off, and BambuBuddy's own
            # terminal events are the authoritative signal. But if the printer
            # sits inactive for a while and no terminal event ever arrives,
            # something dropped one, and holding a tile open forever is worse
            # than closing it on inference.
            first = self._inactive_since.setdefault(serial, now)
            if now - first >= INACTIVE_GRACE_SECONDS:
                inferred = {
                    STATE_FINISH: Outcome.COMPLETED,
                    STATE_FAILED: Outcome.FAILED,
                }.get(status.state, Outcome.ABORTED)
                log.info(
                    "%s has reported %s for %.0fs with no terminal event; closing the tile as %s",
                    serial, status.state, now - first, inferred.value,
                )
                await self._go_terminal(session, inferred, now)
            return

        # Anything else, including transient firmware states like LOADING,
        # holds the tile exactly as it is. `state` is an unvalidated
        # passthrough of the firmware's own string and is not a closed set, so
        # the default branch has to be inert.

    # --- Print lifecycle events -------------------------------------------

    def _loggable(self, name: Optional[str]) -> Optional[str]:
        """A job name as it may appear in the logs.

        Privacy mode is about the model name not being visible, and a log file
        is at least as likely to be shared as a Lock Screen photo. Keeping the
        name off the tile and then writing it to stdout would defeat the
        setting for anyone who pastes their logs into an issue.
        """
        if name and self._config.privacy_mode:
            return "(hidden)"
        return name

    def _spend_dismissal(self, session: SessionState) -> None:
        """A dismissal dies with the print it belonged to.

        Called wherever a session reaches a terminal state, however that was
        established. Without it a swipe would outlive its print by the full
        dismissal TTL and silence the next one.
        """
        if self._store.is_dismissed(session.serial):
            log.debug("Clearing the dismissal on %s: the print it belonged to has ended",
                      session.serial)
            self._store.clear_dismissal(session.serial)

    async def _go_terminal(self, session: SessionState, outcome: Outcome, now: float) -> None:
        """Write the terminal content and start the hold timer.

        The tile is NOT ended here. Ending is irreversible, and an outcome
        inferred from a printer going quiet is not certain enough to deserve
        an irreversible action: a printer that drops off the network mid-print
        looks exactly like one that stopped. Holding for a couple of minutes
        lets a printer that comes back revoke the outcome, and the user sees
        one correct result instead of a wrong one followed by nothing.
        """
        first_terminal = session.outcome is None
        if first_terminal:
            self._spend_dismissal(session)
        session.outcome = outcome
        session.phase = Phase.TERMINAL
        # The hold runs from the FIRST terminal event. A second, better event
        # inside the hold corrects the content but does not restart the clock;
        # it would otherwise be possible for a sequence of contradicting
        # events to keep a finished tile on screen indefinitely.
        if first_terminal or session.terminal_at is None:
            session.terminal_at = now
        session.delivery = Delivery.ENDING if session.activity_id else Delivery.ENDED

        # A FAILURE reported while the printer is still saying RUNNING is the
        # signature of BambuBuddy's restart reconciliation rather than a real
        # ending, so the hold is doubled to give the genuine outcome time to
        # arrive. A SUCCESS is never extended: it already outranks everything,
        # so nothing that arrives later could improve it, and doubling the
        # hold would just leave every finished tile on screen twice as long.
        # Every non-success outcome here is REVOCABLE, and saying so is the
        # whole reason this flag exists.
        #
        # A success is certain: a printer that reported FINISH finished. A
        # failure or an abort is not, because both are read from the printer
        # going inactive or going quiet, and a printer that drops off the
        # network mid-print looks exactly like one that stopped. Marking the
        # session provisional rather than simply extending the hold is what
        # lets a later RUNNING reading take it back: `_on_status` revokes a
        # provisional terminal, and `_expire_terminal_holds` refuses to end
        # one while the printer is still talking.
        #
        # This condition used to also require the newest reading to say
        # RUNNING, which made sense when a terminal could arrive as an event
        # contradicting live telemetry. It cannot now: a terminal is only ever
        # reached BECAUSE the printer stopped saying RUNNING, so that clause
        # made the flag unsettable and quietly disabled every revocation path
        # behind it.
        if first_terminal and not outcome.is_success:
            session.terminal_provisional = True
            log.info(
                "%s reported %s while the printer still says RUNNING; treating that as "
                "provisional until the printer agrees", session.serial, outcome.value,
            )

        if session.activity_id:
            content = terminal_content(session, self._config)
            await self._send(session, content, now, kind="terminal")

        self._store.save_session(session)

        # The push notification, and WHEN it goes, follows the same logic as
        # the tile. A success outranks everything, so "Print finished" goes
        # out immediately. A failure, stop or cancel can still be overruled
        # by a later success during the hold (BambuBuddy's reconciliation
        # routinely synthesises an abort for a print that goes on to finish),
        # so the alert for those waits until the hold expires and the outcome
        # is final. Sending it now and then ending as "Done" would leave a
        # "Print failed" notification in the user's history for a print that
        # succeeded.
        if outcome.is_success and not session.alert_sent:
            session.alert_sent = True
            await self._alert_terminal(
                session.serial, session.printer_name, session.job_name, outcome,
                dedupe_key=session.session_key,
            )

    async def _supersede(self, session: SessionState, now: float) -> None:
        """End a session because a different print took its place."""
        if session.activity_id and session.is_live():
            session.outcome = Outcome.SUPERSEDED
            session.phase = Phase.TERMINAL
            await self._end_tile(session, now, keep_for=0)
        else:
            self._forget(session)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _start_tile(self, session: SessionState, now: float) -> None:
        """Create the Live Activity for a session, or queue it for a slot."""
        if self._store.is_dismissed(session.serial):
            log.debug("Not starting a tile for %s: the last one was dismissed and no new "
                      "print has started since", session.serial)
            session.delivery = Delivery.DISMISSED
            self._store.save_session(session)
            return

        if now < self._start_blocked_until:
            log.info(
                "Not starting a tile for %s yet: %s", session.serial, self._start_blocked_reason
            )
            self._queue_for_slot(session)
            return

        if now < self._gateway_down_until:
            # The gateway was unreachable a moment ago. Every attempt costs a
            # full HTTP timeout on the event loop, so wait it out rather than
            # paying that once per queued printer per tick.
            self._queue_for_slot(session)
            return

        if self._active_tile_count() >= self._config.max_tiles:
            log.info(
                "All %d tile slots are in use; %s is queued for the next one",
                self._config.max_tiles, session.serial,
            )
            self._queue_for_slot(session)
            return

        # A previous start for this session timed out with no answer. The
        # gateway may have created the tile and lost only the response, so
        # before creating another, look for one to adopt.
        if session.start_ambiguous:
            if await self._adopt_orphan(session, now):
                return

        content = build_content(session, self._config, initial=True)

        # Write the intent down BEFORE asking for the tile. If the process
        # dies between the POST and the response, the next run finds a session
        # flagged ambiguous and reconciles against the device's live tiles
        # instead of blindly starting a second one. Persisting after the call,
        # which is the obvious order, is precisely the window that produces
        # two tiles for one print.
        session.start_ambiguous = True
        self._store.save_session(session)

        try:
            started = await self._client.start(content)
        except NotifyTransportError as exc:
            # AMBIGUOUS: the tile may exist. Flag the session so the retry
            # reconciles first, and back the whole device off so a gateway
            # outage does not cost one timeout per queued printer per tick.
            log.warning("Start for %s did not complete (%s); will reconcile and retry",
                        session.serial, exc)
            session.start_ambiguous = True
            self._gateway_down_until = now + GATEWAY_BACKOFF_SECONDS
            self._defer_start(session, now, GATEWAY_BACKOFF_SECONDS)
            return
        except NotifyValidationError as exc:
            if exc.is_tile_cap:
                # The one transient 400. The gateway counts tiles that are
                # still `starting` as well as active ones, so a tile that
                # never appeared holds a slot for fifteen minutes.
                log.info("Notify reports the device is at its tile limit; %s is queued",
                         session.serial)
                session.start_ambiguous = False
                self._queue_for_slot(session)
                return
            log.error("Notify rejected the tile for %s: %s", session.serial, exc)
            session.start_ambiguous = False
            self._forget(session)
            return
        except NotifyThrottled as exc:
            # Apple is silently discarding push-to-start for this device.
            # Waiting alone does NOT clear it: the counter resets when the
            # phone reports a fresh start credential, which happens when the
            # user opens the Notify app.
            self._block_starts(
                now, exc.retry_after,
                "Notify is refusing new tiles for this device because recent ones never "
                "appeared. Open the Notify app once on the phone to refresh its credential, "
                f"then wait {exc.retry_after // 60} minutes.",
                session, level=logging.ERROR,
            )
            return
        except NotifyDeviceNotReady as exc:
            # The phone cannot show tiles and the gateway knows why. Only the
            # human holding it can fix this, so say exactly what it said.
            self._block_starts(
                now, SERVICE_BACKOFF_SECONDS,
                f"The Notify device cannot show Live Activities: {exc} (open the Notify app "
                "once, and check Settings -> Notify -> Live Activities)",
                session, level=logging.ERROR,
            )
            return
        except NotifyAuthError:
            # Queued rather than abandoned: credentials get fixed without a
            # restart, and this path is also reachable mid-rollover, where
            # dropping the session would leave a running print tile-less.
            self._block_starts(
                now, SERVICE_BACKOFF_SECONDS,
                "Notify rejected the credentials. Check NOTIFY_DEVICE_ID and "
                "NOTIFY_DEVICE_TOKEN against Settings -> Device in the Notify app.",
                session, level=logging.ERROR,
            )
            return
        except (NotifyDisabled, NotifyUpstreamError) as exc:
            self._block_starts(
                now, SERVICE_BACKOFF_SECONDS,
                f"Notify cannot start tiles right now ({exc}); retrying later",
                session, level=logging.WARNING,
            )
            return
        except NotifyError as exc:
            log.error("Unexpected error starting a tile for %s: %s", session.serial, exc)
            session.start_ambiguous = False
            self._queue_for_slot(session)
            return

        self._adopt(session, started.activity_id, started.expires_at, now, content)
        log.info("Started tile %s for %s", started.activity_id, session.serial)

    def _adopt(self, session: SessionState, activity_id: str, expires_at: Optional[str],
               now: float, content: dict) -> None:
        """Bind a tile to a session, whether freshly started or found orphaned."""
        session.activity_id = activity_id
        session.expires_at = parse_timestamp(expires_at)
        session.delivery = Delivery.ACTIVE
        session.start_ambiguous = False
        session.last_sent_at = now
        session.last_progress_sent_at = now
        # Hash the UPDATE-shaped payload, not the start-shaped one. A start
        # carries `symbol` and `tint`, which no update ever repeats, so hashing
        # what was sent would guarantee a mismatch on the very next frame and
        # buy a redundant push for every tile.
        session.last_payload_hash = payload_hash(
            build_content(session, self._config, initial=False)
        )
        session.last_sent_signature = self._significance(session, content)
        commit_sent_eta(session, content)
        self._store.save_session(session)

        # A tile that is accepted but never appears is invisible without this
        # check, and each one pushes the device closer to the push-to-start
        # throttle.
        self._verify_at[activity_id] = now + self._config.start_verify_after_s

    async def _adopt_orphan(self, session: SessionState, now: float) -> bool:
        """Look for a tile the gateway created for a start whose response was lost.

        Lists the device's live tiles and adopts one that no session owns and
        whose title matches the one this printer's tile would carry. The list
        response includes each tile's content, which is what makes the match
        possible.

        Returns True when the caller must NOT go on to start a tile: either
        one was adopted, or the list could not be read and starting blindly
        would risk a duplicate. False means the start really did not happen
        and a fresh tile is safe.
        """
        try:
            live = await self._client.list_device()
        except NotifyError as exc:
            # Cannot reconcile right now. Do NOT start blindly: stay ambiguous
            # and try again next time.
            log.debug("Could not list tiles to reconcile %s: %s", session.serial, exc)
            self._defer_start(session, now, GATEWAY_BACKOFF_SECONDS)
            return True

        owned = {s.activity_id for s in self._sessions.values() if s.activity_id}
        # Compare against the title as BUILT, not the raw printer name: the
        # content builder collapses whitespace, strips control characters and
        # truncates by UTF-16 units, so a printer called "Office  X1C" or one
        # with an emoji in its name would never match its own tile.
        wanted = build_content(session, self._config, initial=True)
        wanted_title = wanted.get("title")
        candidates = []
        for activity in live:
            if activity.activity_id in owned:
                continue
            title = (activity.raw.get("content") or {}).get("title")
            if wanted_title and title == wanted_title:
                candidates.append(activity)

        if len(candidates) == 1:
            activity = candidates[0]
            log.info("Adopting orphaned tile %s for %s", activity.activity_id, session.serial)
            self._adopt(session, activity.activity_id, activity.expires_at, now, wanted)
            return True

        if len(candidates) > 1:
            # Two unowned tiles both claiming to be this printer. The title is
            # the only correlation the gateway offers and it is not unique, so
            # guessing risks driving somebody else's tile. Leave them for the
            # gateway's own housekeeping and start a fresh one.
            log.warning(
                "Found %d unowned tiles titled %r; cannot tell which belongs to %s, "
                "so starting a new one", len(candidates), wanted_title, session.serial,
            )

        # Nothing to adopt: the start really did not happen.
        session.start_ambiguous = False
        return False

    def _block_starts(self, now: float, delay: float, reason: str,
                      session: Optional[SessionState], level: int = logging.WARNING) -> None:
        # Every caller reached here because the gateway ANSWERED and refused,
        # so no tile was created and there is nothing to reconcile.
        """Stop starting tiles for a while, say why once, and queue the session.

        One helper for every condition that affects the DEVICE rather than one
        print: a throttle, a device that cannot show tiles, rejected
        credentials, a kill switch. They differ only in how long and why.
        """
        until = now + delay
        if until > self._start_blocked_until:
            self._start_blocked_until = until
            self._start_blocked_reason = reason
        log.log(level, "%s", reason)
        if session is not None:
            session.start_ambiguous = False
            self._queue_for_slot(session)

    def _defer_start(self, session: SessionState, now: float, delay: float) -> None:
        """Queue a start and hold it back for a while.

        Distinct from a plain slot queue: this one is for a start that FAILED
        rather than one that has nowhere to go, so retrying it immediately
        would just fail again, at the cost of a blocking HTTP timeout every
        tick.
        """
        self._retry_start_after[session.serial] = now + delay
        self._queue_for_slot(session)

    def _queue_for_slot(self, session: SessionState) -> None:
        session.delivery = Delivery.PENDING
        self._store.save_session(session)
        if session.serial not in self._waiting_for_slot:
            self._waiting_for_slot.append(session.serial)

    def _active_tile_count(self) -> int:
        return sum(
            1 for session in self._sessions.values()
            if session.activity_id and session.delivery in (Delivery.ACTIVE, Delivery.ENDING)
        )

    async def _maybe_send(self, session: SessionState, now: float, force: bool = False) -> None:
        """Send an update if this one is worth an APNs push."""
        if not session.activity_id or session.delivery is not Delivery.ACTIVE:
            return

        content = build_content(session, self._config, initial=False)
        digest = payload_hash(content)

        if not force:
            if digest == session.last_payload_hash:
                return
            # Progress ticks are the bulk of the traffic and the least urgent,
            # so they wait out a floor. The gateway coalesces to one push per
            # ten seconds anyway; this keeps the HTTP requests down too.
            #
            # A change of MEANING is exempt. A pause, a lost connection, a
            # finished print, or a countdown appearing for the first time is
            # news, and holding news behind a rate limit built for percentages
            # makes the tile feel broken.
            if self._significance(session, content) == session.last_sent_signature and \
                    now - session.last_progress_sent_at < self._config.progress_min_interval_s:
                return

        await self._send(session, content, now, kind="update")

    @staticmethod
    def _significance(session: SessionState, content: dict) -> tuple:
        """What this tile MEANS, as opposed to what it says.

        Two payloads with the same significance differ only in numbers that
        tick, so the later one can wait for the pacing floor. Two with
        different significance differ in something a user would notice, so the
        later one goes now.
        """
        return (
            session.phase.value,
            content.get("status"),
            # Whether a countdown would be on the TILE after this payload,
            # which is not the same as whether `endsIn` is in it: an unchanged
            # estimate is deliberately omitted, and that leaves the existing
            # countdown alone rather than removing it.
            countdown_after(session, content),
        )

    async def _send(self, session: SessionState, content: dict, now: float,
                    kind: str) -> None:
        """Push content to a tile and handle everything that can go wrong."""
        if kind != "terminal" and now < self._gateway_down_until:
            # The gateway was unreachable a moment ago. A routine update can
            # wait; paying a full HTTP timeout for every paced frame would
            # serialise the event loop behind the outage. Terminal content
            # still tries, because ending is the one thing worth the wait.
            return
        try:
            await self._client.update(session.activity_id, content)
        except NotifyGone as exc:
            await self._handle_gone(session, exc, now)
            return
        except NotifyAuthError:
            # The gateway answers a bad token and an unknown id identically,
            # by design, so this cannot tell "your credentials are wrong" from
            # "that activity is gone". Guessing the second and dropping the id
            # would orphan a live tile every time the token was the problem.
            # Ask the gateway which world we are in: if the tile is in the
            # device's live list, the credentials are fine and the tile is
            # ours; if the list itself is refused, the credentials are wrong
            # and nothing should be forgotten.
            try:
                live = {a.activity_id for a in await self._client.list_device()}
            except NotifyError:
                log.error(
                    "Notify rejected an update for %s AND refused to list the device. "
                    "That points at the credentials rather than the tile; keeping the "
                    "session so nothing is orphaned.", session.serial,
                )
                return
            if session.activity_id in live:
                log.warning("Notify rejected an update for tile %s but still lists it as "
                            "live; leaving it alone", session.activity_id)
                return
            log.warning("Notify no longer recognises tile %s for %s; dropping it",
                        session.activity_id, session.serial)
            session.activity_id = None
            session.delivery = Delivery.ENDED
            self._store.save_session(session)
            return
        except NotifyTransportError as exc:
            log.debug("Update for %s failed (%s); backing off", session.serial, exc)
            self._gateway_down_until = now + GATEWAY_BACKOFF_SECONDS
            return
        except NotifyError as exc:
            # Includes the kill switch (503), which is transient by definition:
            # keep the session, keep the tile, try again on the next tick.
            log.debug("Update for %s deferred (%s); will retry", session.serial, exc)
            return

        # A successful round trip means the gateway is back.
        self._gateway_down_until = 0.0

        session.last_sent_at = now
        session.last_payload_hash = payload_hash(content)
        session.last_sent_signature = self._significance(session, content)
        # Only NOW is the countdown genuinely on the tile.
        commit_sent_eta(session, content)
        if kind == "update":
            session.last_progress_sent_at = now
        self._store.save_session(session)

    async def _handle_gone(self, session: SessionState, error: NotifyGone, now: float) -> None:
        """Decide what a 410 actually meant.

        The whole point of this method: `dismissed` is the user's decision and
        must be honoured forever, while `overdue`, `abandoned`, `expired` and
        `never-started` are the gateway tidying up a tile whose print is very
        possibly still running. Treating the second group like the first is
        how an integration silently stops working for a printer whose ETA
        happened to overrun by half an hour.
        """
        classified = await self._client.classify_gone(session.activity_id or "", error)
        reason = classified.end_reason

        if reason is None:
            # The follow-up that would have said WHY failed. Deciding either
            # way is wrong: assume dismissal and a reaped tile never comes
            # back; assume reaping and a tile the user swiped away reappears,
            # which is the whack-a-mole the gateway's own design exists to
            # prevent. So decide nothing. The tile is already gone, the
            # session keeps its identity, and the next tick asks again.
            log.info("Tile for %s is gone but the reason could not be read; will ask again",
                     session.serial)
            session.pending_gone = True
            self._store.save_session(session)
            return
        session.pending_gone = False

        if classified.user_dismissed:
            log.info("Tile for %s was dismissed by the user; not recreating it for this print",
                     session.serial)
            self._store.record_dismissal(session.serial)
            session.delivery = Delivery.DISMISSED
            session.activity_id = None
            self._store.save_session(session)
            return

        if reason == "script":
            session.delivery = Delivery.ENDED
            session.activity_id = None
            self._store.save_session(session)
            return

        log.info(
            "Notify reaped the tile for %s (%s); recreating it because the print is still live",
            session.serial, reason,
        )
        session.activity_id = None
        session.delivery = Delivery.PENDING
        self._store.save_session(session)
        if session.phase is not Phase.TERMINAL:
            await self._start_tile(session, now)

    def _forget(self, session: SessionState) -> None:
        """Retire a session and drop every reference to it.

        One place, because a session leaks through more routes than is
        obvious: the serial can still be queued for a tile slot, the activity
        id can still be awaiting verification, and the per-serial timers can
        still be armed. Any one left behind either wastes a slot or, worse,
        parks a dead entry in `_sessions` that blocks the status-topic
        creation path for that printer permanently.
        """
        session.delivery = Delivery.ENDED
        self._store.save_session(session)
        if self._sessions.get(session.serial) is session:
            self._sessions.pop(session.serial, None)
        if session.serial in self._waiting_for_slot:
            self._waiting_for_slot.remove(session.serial)
        if session.activity_id:
            self._verify_at.pop(session.activity_id, None)
        self._running_since.pop(session.serial, None)
        self._inactive_since.pop(session.serial, None)
        self._retry_start_after.pop(session.serial, None)

    async def _end_tile(self, session: SessionState, now: float, keep_for: int) -> bool:
        """End a tile for good, and send the outcome's alert if it is still owed.

        Returns False when the gateway could not be reached, in which case the
        session is LEFT IN ENDING and nothing is forgotten. That matters more
        than it looks: treating a timed-out DELETE as success clears the
        activity id, and the tile is then still live on the user's Lock Screen
        with nothing left in this process that can ever update or end it. The
        retry runs from `_retry_pending_ends` on the next tick, and `end` is
        idempotent so repeating it is free.
        """
        if session.activity_id:
            try:
                await self._client.end(
                    session.activity_id, terminal_content(session, self._config), keep_for
                )
            except NotifyGone:
                # Already gone. Ending is idempotent and this is not an error.
                pass
            except (NotifyTransportError, NotifyDisabled, NotifyUpstreamError) as exc:
                # Transient. The tile is very probably still up there.
                log.warning(
                    "Could not end tile %s (%s); keeping it and retrying",
                    session.activity_id, exc,
                )
                session.delivery = Delivery.ENDING
                session.end_keep_for = keep_for
                self._ends_owed[session.activity_id] = session
                self._gateway_down_until = max(
                    self._gateway_down_until, now + GATEWAY_BACKOFF_SECONDS
                )
                self._store.save_session(session)
                return False
            except NotifyError as exc:
                # A 400 or 403: the request will not become valid by repeating
                # it, and the gateway's own housekeeping reaps the tile in the
                # end. Close the session rather than retrying forever.
                log.warning("Notify refused to end tile %s (%s); giving up on it",
                            session.activity_id, exc)
            else:
                log.info("Ended tile %s for %s (%s)", session.activity_id, session.serial,
                         session.outcome.value if session.outcome else "no outcome")

        # The deferred alert for a non-success outcome goes out now that the
        # hold has expired and nothing can overrule it.
        #
        # `outcome_inferred` means "nothing was observed; this outcome was
        # assumed because the printer vanished". That is the abandon path, and
        # it must NOT produce a "Print stopped" notification: it would be a
        # guess, and the "not responding" alert has already said everything
        # that is actually known. An outcome read from a state the printer
        # genuinely reported is not that, and does alert.
        if session.outcome is not None and not session.alert_sent and \
                not session.outcome_inferred:
            session.alert_sent = True
            await self._alert_terminal(
                session.serial, session.printer_name, session.job_name, session.outcome,
                dedupe_key=session.session_key,
            )

        # `_forget` before the id is cleared, so it can drop the verification
        # entry keyed on that id.
        self._ends_owed.pop(session.activity_id, None)
        self._forget(session)
        session.activity_id = None
        return True

    # ------------------------------------------------------------------
    # Periodic work
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Everything that happens on a timer rather than on a message."""
        now = time.time()
        await self._expire_terminal_holds(now)
        await self._retry_pending_ends(now)
        await self._resolve_pending_gone(now)
        await self._detect_silence(now)
        await self._keepalive(now)
        await self._verify_starts(now)
        await self._rollover(now)
        await self._drain_slot_queue(now)

    async def _expire_terminal_holds(self, now: float) -> None:
        for session in list(self._sessions.values()):
            if session.phase is not Phase.TERMINAL or session.terminal_at is None:
                continue
            if now - session.terminal_at < self._config.terminal_hold_s:
                continue

            if session.terminal_provisional:  # noqa: SIM102 - read with the block below
                # A failure reported while the printer was still running. Do
                # NOT end it on a timer: the print may well be going, and
                # ending it here is what made a mid-print network blip wipe a
                # tile that had four hours left. It ends when the printer
                # goes quiet or reports an inactive state, both of which are
                # handled elsewhere and both of which are real evidence.
                latest = self._latest_status.get(session.serial)
                if latest is None:
                    # No telemetry seen at all yet, which after a restart is
                    # every session. Finalising here would end a live print's
                    # tile on the first tick, which is precisely the failure
                    # the provisional state exists to prevent. Wait for the
                    # printer to say something; the abandon timer bounds it.
                    if now - self._started_at < self._config.silence_after_s:
                        continue
                elif latest.state in ACTIVE_STATES and \
                        now - latest.timestamp.timestamp() < self._config.silence_after_s:
                    # RUNNING or PAUSE. A paused print is still in flight, and
                    # treating a pause as "the printer stopped confirming"
                    # would finalise an abort for a print sitting on a
                    # filament change.
                    continue
                log.info("%s stopped confirming the print; the provisional outcome stands",
                         session.serial)
                session.terminal_provisional = False

            await self._end_tile(session, now, keep_for=self._config.keep_for)

    async def _retry_pending_ends(self, now: float) -> None:
        """Finish ending tiles whose DELETE could not be delivered.

        Driven off `_ends_owed` rather than `_sessions`, because the session
        may well have been evicted by the next print on the same printer by
        the time the gateway comes back. Ending is idempotent, so retrying
        costs nothing, and the alternative (assuming success) leaves a live
        tile on the Lock Screen that nothing can ever touch again.
        """
        if now < self._gateway_down_until:
            return
        for activity_id, session in list(self._ends_owed.items()):
            if not session.activity_id:
                self._ends_owed.pop(activity_id, None)
                continue
            await self._end_tile(session, now, keep_for=session.end_keep_for)

    async def _resolve_pending_gone(self, now: float) -> None:
        """Ask again why a tile vanished, when the first attempt could not say.

        Until the answer arrives the session sits in limbo: no tile, and no
        decision about whether it may have another. That is the correct place
        to be, because both defaults are wrong in a way the user notices.
        """
        if now < self._gateway_down_until:
            return
        for session in list(self._sessions.values()):
            if not session.pending_gone or not session.activity_id:
                continue
            try:
                state = await self._client.status(session.activity_id)
            except NotifyError as exc:
                log.debug("Still cannot read why %s vanished: %s", session.activity_id, exc)
                continue
            reason = state.end_reason or (
                "dismissed" if state.state == "dismissed" else None
            )
            if reason is None:
                continue
            await self._handle_gone(
                session, NotifyGone("resolved", 410, {}, end_reason=reason), now
            )

    async def _detect_silence(self, now: float) -> None:
        """Notice a printer that stopped talking.

        Silence is derived from when telemetry last ARRIVED, not from the
        `connected` flag, because that flag frequently never flips. The final
        `connected: false` publish can be dropped by BambuBuddy's own
        BambuBuddy's own staleness detection only runs when something asks it
        for a status, and after a smart-plug Auto Off the printer stops
        reporting entirely. The result is a printer that answers `RUNNING, 96%`
        forever, which nothing else in this service would ever retire.
        """
        # A copy, because the abandon branch below retires sessions.
        for session in list(self._sessions.values()):
            if not session.is_live() or session.phase is Phase.TERMINAL:
                continue
            if session.last_status_ts <= 0:
                continue
            if now - session.last_status_ts < self._config.silence_after_s:
                continue
            silent_for = now - session.last_status_ts

            if silent_for >= self._config.abandon_after_s:
                # Silence has an exit, and it has to. A printer switched off at
                # the wall publishes nothing ever again, so no terminal event
                # is coming; without this the keepalive would faithfully hold a
                # "Connection lost" tile on the Lock Screen until Apple's
                # eight-hour ceiling, and the rollover would then start a fresh
                # one and do it again.
                log.info(
                    "No telemetry from %s for %.0fs; giving up and ending the tile",
                    session.serial, silent_for,
                )
                session.outcome_inferred = True
                await self._go_terminal(session, Outcome.ABORTED, now)
                continue

            if session.phase is Phase.SILENT:
                continue
            log.info(
                "No telemetry from %s for %.0fs; marking the tile as connection lost",
                session.serial, silent_for,
            )
            session.phase = Phase.SILENT
            await self._maybe_send(session, now, force=True)
            if self._alerts:
                await self._alerts.printer_silent(
                    session.serial, session.printer_name, session.job_name
                )

    async def _keepalive(self, now: float) -> None:
        """Refresh every live tile on a timer, whether or not anything changed.

        This deliberately BYPASSES the redundant-payload check. The gateway
        ends a progress-only tile that has gone two hours without an update,
        and a tile whose countdown has been cleared (any paused, silent or
        ETA-unknown print) is exactly that shape. An unchanged tile is the
        case where the keepalive matters most, so making it conditional on
        change would defeat its only purpose.
        """
        # A copy: a 410 during a keepalive can retire or recreate a session.
        for session in list(self._sessions.values()):
            if not session.is_live() or session.phase is Phase.TERMINAL:
                continue
            if not session.activity_id:
                continue
            if now - session.last_sent_at < self._config.keepalive_interval_s:
                continue
            await self._send(
                session, build_content(session, self._config, initial=False), now,
                kind="keepalive",
            )

    async def _verify_starts(self, now: float) -> None:
        """Confirm that started tiles actually appeared on the phone.

        A start can be accepted by the gateway and by Apple and still never
        produce a tile, because iOS silently throttles push-to-start once the
        device's start credential goes stale, which happens whenever the app
        is updated or reinstalled. Nothing anywhere reports this. The gateway
        notices after fifteen minutes and, after two such losses, starts
        refusing with a 429 and an escalating quiet period.

        Checking at ninety seconds turns a silent failure into one actionable
        log line. What it does NOT do is end the tile and try again: that
        would be one discarded push every ninety seconds forever, which keeps
        Apple's throttle shut, and it would forfeit the gateway's own recovery,
        which re-sends any start still inside its fifteen-minute window the
        moment the phone reports a fresh credential. So the tile is left
        alone, further starts are backed off with an escalating delay, and the
        only time a stuck tile is ended is when another print is actually
        waiting for its slot.
        """
        if now < self._gateway_down_until:
            # Every check costs a full HTTP timeout while the gateway is
            # unreachable, and the deadline is deliberately kept on failure,
            # so without this guard a five-printer farm spends every tick
            # blocking the event loop while the poll queue backs up.
            return

        for activity_id, deadline in list(self._verify_at.items()):
            if now < deadline:
                continue

            session = next(
                (s for s in self._sessions.values() if s.activity_id == activity_id), None
            )
            if session is None or session.phase is Phase.TERMINAL:
                # Either the tile is gone, or the print has finished and its
                # content is on screen for the hold. Re-queuing a finished
                # print here would push a brand new tile for it after it ended.
                self._verify_at.pop(activity_id, None)
                continue

            try:
                state = await self._client.status(activity_id)
            except NotifyTransportError as exc:
                # Kept on the schedule (one blip must not disarm the check
                # that catches silently discarded tiles) but the whole device
                # backs off, so the retry is not every tick.
                log.debug("Could not verify tile %s (%s); will retry", activity_id, exc)
                self._gateway_down_until = now + GATEWAY_BACKOFF_SECONDS
                return
            except NotifyError as exc:
                log.debug("Could not verify tile %s (%s); will retry", activity_id, exc)
                continue

            self._verify_at.pop(activity_id, None)

            if state.is_confirmed:
                # The device is receiving tiles. Whatever trouble there was is
                # over, so the escalation starts from scratch next time.
                self._unconfirmed_starts = 0
                continue

            if state.state != "starting":
                # Ended or dismissed already; the next update will hit a 410
                # and be classified properly there.
                continue

            self._unconfirmed_starts += 1
            delay = UNCONFIRMED_START_BACKOFF[
                min(self._unconfirmed_starts, len(UNCONFIRMED_START_BACKOFF)) - 1
            ]
            self._block_starts(
                now, delay,
                f"Tile {activity_id} for {session.serial} was accepted but has not appeared "
                "on the phone. This usually means iOS is discarding Live Activity starts "
                "because Notify's start credential is stale: open the Notify app once on the "
                f"device. Not starting any more tiles for {int(delay // 60)} minutes.",
                None, level=logging.ERROR,
            )

            if self._waiting_for_slot:
                # Something else wants the slot this ghost is holding. Ending
                # it costs the gateway's auto-resend for this one tile, which
                # is the lesser loss when a real print is queued behind it.
                log.info("Ending unconfirmed tile %s to free its slot for a waiting print",
                         activity_id)
                try:
                    await self._client.end(activity_id, None, 0)
                except NotifyError:
                    pass
                session.activity_id = None
                session.delivery = Delivery.PENDING
                self._store.save_session(session)
                self._queue_for_slot(session)

    async def _rollover(self, now: float) -> None:
        """Replace a tile that is approaching Apple's eight-hour ceiling.

        Apple ends every Live Activity at eight hours regardless of what the
        countdown said, and long prints routinely outlast that. Rolling over
        early rather than racing the ceiling avoids a gap on the Lock Screen,
        and the OLD tile is ended BEFORE the successor is started because the
        gateway's five-tile cap counts tiles that are merely starting as well
        as active ones: overlapping would risk the successor being refused.
        """
        for session in list(self._sessions.values()):
            if not session.is_live() or session.phase is Phase.TERMINAL:
                continue
            if not session.activity_id or session.created_at <= 0:
                continue
            # Apple's ceiling is measured from when the tile STARTED, which
            # the gateway echoes back as `expiresAt`. Prefer that to our own
            # clock: a tile adopted after a restart or reconciled from an
            # orphan may be older than this session thinks it is.
            if session.expires_at is not None:
                margin = 8 * 3600 - self._config.rollover_after_s
                if now < session.expires_at.timestamp() - margin:
                    continue
            elif now - session.created_at < self._config.rollover_after_s:
                continue
            if self._store.is_dismissed(session.serial):
                continue

            log.info(
                "Tile for %s is approaching Apple's 8 hour limit; rolling it over",
                session.serial,
            )
            old_id = session.activity_id
            try:
                await self._client.end(old_id, None, 0)
            except NotifyGone:
                pass
            except (NotifyTransportError, NotifyDisabled, NotifyUpstreamError) as exc:
                # Ambiguous: the old tile may well still be live. Starting a
                # successor now would leave TWO tiles for one print, both
                # counting against the five-tile cap. Try again next tick.
                log.warning("Could not end %s before rollover (%s); deferring", old_id, exc)
                self._gateway_down_until = max(
                    self._gateway_down_until, now + GATEWAY_BACKOFF_SECONDS
                )
                continue
            except NotifyError as exc:
                log.debug("Notify refused to end %s before rollover: %s", old_id, exc)
            self._verify_at.pop(old_id, None)
            session.activity_id = None
            session.delivery = Delivery.PENDING
            # The successor is a continuation, not a new print, so the session
            # keeps its key, its job name and its progress. Only the tile's
            # own clock restarts.
            session.created_at = now
            session.last_payload_hash = None
            await self._start_tile(session, now)

    async def _drain_slot_queue(self, now: float) -> None:
        """Start tiles that were waiting for a free slot.

        Ordered by how close each print is to finishing, so a job with twenty
        minutes left gets the freed slot before one with six hours. First
        come, first served would be worse: the print most worth watching is
        usually the one about to need attention.
        """
        if not self._waiting_for_slot:
            return
        if now < self._start_blocked_until or now < self._gateway_down_until:
            return
        if self._active_tile_count() >= self._config.max_tiles:
            return

        def sort_key(serial: str) -> tuple:
            session = self._sessions.get(serial)
            if session is None:
                return (2, float("inf"))
            # A paused or silent print's ETA is frozen at whatever it said when
            # it stopped moving, so it would otherwise outrank a running print
            # that is genuinely closer to finishing. Running prints first,
            # then by remaining time.
            running = 0 if session.phase is Phase.PRINTING else 1
            eta = float(session.eta_seconds) if session.eta_seconds is not None else float("inf")
            return (running, eta)

        for serial in sorted(list(self._waiting_for_slot), key=sort_key):
            session = self._sessions.get(serial)
            if session is None or not session.is_live() or session.activity_id:
                self._waiting_for_slot.remove(serial)
                self._retry_start_after.pop(serial, None)
                continue
            if now < self._retry_start_after.get(serial, 0.0):
                continue
            if self._active_tile_count() >= self._config.max_tiles:
                break
            self._waiting_for_slot.remove(serial)
            self._retry_start_after.pop(serial, None)
            await self._start_tile(session, now)

    # ------------------------------------------------------------------
    # Alerts and diagnostics
    # ------------------------------------------------------------------

    async def _alert_terminal(self, serial: str, printer_name: Optional[str],
                              job_name: Optional[str], outcome: Outcome,
                              dedupe_key: Optional[str] = None) -> None:
        if self._alerts:
            await self._alerts.print_finished(serial, printer_name, job_name, outcome,
                                              dedupe_key=dedupe_key)

    def snapshot(self) -> dict:
        """State for the health endpoint."""
        now = time.time()
        # `list()` first: this runs on the health server's thread while the
        # event loop may be adding or retiring sessions, and iterating a dict
        # that changes size raises mid-way. The copy is atomic under the GIL.
        sessions = list(self._sessions.values())
        waiting = list(self._waiting_for_slot)
        return {
            "sessions": [
                {
                    "serial": session.serial,
                    "printer": session.printer_name,
                    "job": session.job_name if not self._config.privacy_mode else None,
                    "queue_job_id": session.job_id,
                    "phase": session.phase.value,
                    "delivery": session.delivery.value,
                    "activity_id": session.activity_id,
                    "progress": round(session.progress, 1),
                    "eta_seconds": session.eta_seconds,
                    "age_seconds": round(now - session.created_at, 1) if session.created_at else None,
                    "seconds_since_telemetry": (
                        round(now - session.last_status_ts, 1) if session.last_status_ts else None
                    ),
                }
                for session in sessions
            ],
            "tiles_in_use": sum(
                1 for session in sessions
                if session.activity_id and session.delivery in (Delivery.ACTIVE,
                                                                Delivery.ENDING)
            ),
            "tile_limit": self._config.max_tiles,
            "waiting_for_slot": waiting,
            "unconfirmed_starts": self._unconfirmed_starts,
            "gateway_backoff_for": (
                round(self._gateway_down_until - now, 1) if now < self._gateway_down_until else 0
            ),
            "starts_blocked_for": (
                round(self._start_blocked_until - now, 1)
                if now < self._start_blocked_until else 0
            ),
            "starts_blocked_reason": (
                self._start_blocked_reason if now < self._start_blocked_until else None
            ),
        }
