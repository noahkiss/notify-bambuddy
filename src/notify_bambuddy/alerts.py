# ---------------------------------------------------------------------------
# Ordinary push notifications, alongside the tiles.
#
# A Live Activity is the right shape for "this print is 62% done, 40 minutes
# left". It is the wrong shape for "your print failed two hours ago", because
# a tile ends and vanishes, and Live Activity traffic is deliberately kept OUT
# of Notify's history: a printer pushing percent every few minutes would bury
# everything else the user actually wants to look back at. That is the right
# call for progress and the wrong one for the handful of moments that matter.
#
# So the moments that matter get a real notification too. Three of them:
#
#   a print finished, whatever the outcome
#   the printer ran out of filament
#   the printer stopped responding mid-print
#
# The first is the reason people watch prints at all. The other two are the
# ones worth interrupting a Focus mode for, and they are sent Time Sensitive
# so they do. A successful completion never is: nobody needs to be woken at
# 3am by good news.
#
# Everything here is best effort. A failed alert is logged and dropped, never
# retried and never allowed to propagate, because a notification that did not
# send must not take a tile down with it.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import Config
from .models import Outcome
from .notify_client import NotifyClient, NotifyError

log = logging.getLogger(__name__)

#: Minimum gap between two alerts of the same kind for the same printer. The
#: error topic is a delta that can re-fire as codes clear and reappear, and a
#: flapping network link can bounce a printer in and out of silence. Neither
#: should be able to produce a stream of notifications.
COOLDOWN_SECONDS = 900.0

#: Human wording for each outcome. `ABORTED` is deliberately vaguer than
#: `CANCELLED`: BambuBuddy can only label a stop "cancelled" when it issued
#: the stop itself, so a stop from the printer's own touchscreen arrives as
#: "aborted" and claiming the user cancelled it would sometimes be wrong.
_OUTCOME_TEXT = {
    Outcome.COMPLETED: ("Print finished", "finished"),
    Outcome.FAILED: ("Print failed", "failed"),
    Outcome.CANCELLED: ("Print cancelled", "was cancelled"),
    Outcome.ABORTED: ("Print stopped", "stopped before finishing"),
    Outcome.SUPERSEDED: ("Print replaced", "was replaced by another job"),
}


class Alerts:
    """Sends ordinary push notifications according to the configured policy."""

    def __init__(self, config: Config, client: NotifyClient) -> None:
        self._config = config
        self._client = client
        #: (serial, kind) -> epoch of the last alert of that kind.
        self._last_sent: dict[tuple[str, str], float] = {}

    # --- Policy ------------------------------------------------------------

    def _wants(self, problem: bool) -> bool:
        """Whether this alert passes the configured level.

        "problems" covers failures, runouts and silence. "all" adds successful
        completions, which is the default because "your print is done" is the
        single most requested notification in 3D printing.
        """
        if self._config.alerts == "off":
            return False
        if self._config.alerts == "problems":
            return problem
        return True

    def _cooled_down(self, serial: str, kind: str, now: float) -> bool:
        last = self._last_sent.get((serial, kind))
        if last is not None and now - last < COOLDOWN_SECONDS:
            return False
        self._last_sent[(serial, kind)] = now
        # Keyed entries accumulate one per print; drop anything older than the
        # cooldown so the dict stays the size of "recent alerts".
        if len(self._last_sent) > 256:
            self._last_sent = {k: v for k, v in self._last_sent.items()
                               if now - v < COOLDOWN_SECONDS}
        return True

    # --- Alerts ------------------------------------------------------------

    async def print_finished(self, serial: str, printer_name: Optional[str],
                             job_name: Optional[str], outcome: Outcome,
                             dedupe_key: Optional[str] = None) -> None:
        """One alert per finished print.

        `dedupe_key` identifies the PRINT (the coordinator passes its session
        key), so the cooldown cannot swallow a real failure because the
        previous print on the same printer ended a few minutes earlier. The
        cooldown still applies when no key is given, which is the path for a
        print this service never tracked.
        """
        if outcome is Outcome.SUPERSEDED:
            # An internal bookkeeping outcome, not something that happened to
            # the user's print. Never worth a notification.
            return
        problem = not outcome.is_success
        if not self._wants(problem):
            return

        title, verb = _OUTCOME_TEXT.get(outcome, ("Print ended", "ended"))
        printer = printer_name or serial
        if job_name and not self._config.privacy_mode:
            text = f"{job_name} {verb} on {printer}."
        else:
            text = f"The print on {printer} {verb}."

        await self._send(serial, "terminal", title, text,
                         time_sensitive=problem and self._config.alerts_time_sensitive,
                         dedupe_key=dedupe_key)

    async def printer_silent(self, serial: str, printer_name: Optional[str],
                             job_name: Optional[str]) -> None:
        """The printer stopped reporting while a print was in flight.

        NOT sent Time Sensitive even when problem alerts are. A brief network
        blip produces exactly this, and an alert that interrupts a Focus mode
        needs to be right more often than this one can be.
        """
        if not self._wants(problem=True):
            return
        printer = printer_name or serial
        text = (
            f"{printer} stopped reporting while printing {job_name}. It may have been "
            "switched off, or lost its network connection."
            if job_name and not self._config.privacy_mode
            else f"{printer} stopped reporting while a print was running."
        )
        await self._send(serial, "silent", "Printer not responding", text,
                         time_sensitive=False)

    # --- Delivery ----------------------------------------------------------

    async def _send(self, serial: str, kind: str, title: str, text: str,
                    time_sensitive: bool, dedupe_key: Optional[str] = None) -> None:
        now = time.time()
        # The cooldown is per (printer, kind) unless the caller names the
        # specific print, in which case it is per print: two different prints
        # on one machine are two different pieces of news.
        cooldown_kind = f"{kind}:{dedupe_key}" if dedupe_key else kind
        if not self._cooled_down(serial, cooldown_kind, now):
            log.debug("Suppressing a repeat %s alert for %s", kind, serial)
            return
        try:
            await self._client.notify(
                text=text,
                title=title,
                # One iOS thread per printer, so a farm's notifications stack
                # per machine instead of scattering down the list.
                group_type=f"bambuddy-{serial}",
                time_sensitive=time_sensitive,
            )
        except NotifyError as exc:
            # Best effort by design. The tile is the primary channel; a failed
            # alert must never become an exception the coordinator has to
            # handle mid-state-transition.
            log.warning("Could not send the %s alert for %s: %s", kind, serial, exc)
