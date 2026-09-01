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

#: Human wording for each outcome. `ABORTED` is deliberately vague: it covers
#: every way a print can stop without reporting FINISH or FAILED, including a
#: printer switched off at the wall, and claiming to know which would
#: sometimes be wrong.
_OUTCOME_TEXT = {
    Outcome.COMPLETED: ("Print finished", "finished"),
    Outcome.FAILED: ("Print failed", "failed"),
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

    def _urgent(self, urgent: bool) -> bool:
        """Whether an alert may break through Do Not Disturb.

        `ALERTS_TIME_SENSITIVE=0` is someone saying they do not want this
        service piercing a Focus mode, and it has to hold for the condition
        alerts too. They were added sending time_sensitive directly and so
        overrode a setting the user had explicitly turned off.
        """
        return urgent and self._config.alerts_time_sensitive

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

    async def print_started(self, serial: str, printer_name: Optional[str],
                            job_name: Optional[str], eta_seconds: Optional[int],
                            dedupe_key: Optional[str] = None) -> None:
        """A print began.

        Sent ONLY when this service cannot put a tile on the screen, which
        today means the destination is a browser rather than an iPhone. With a
        tile there is nothing to announce: the tile appearing IS the
        announcement, and a push saying the same thing would be noise on top
        of it.

        Without one, this is the only confirmation the user gets that the
        service is watching at all, and the difference between "it works" and
        "I think it is broken" is exactly this notification.

        Not a problem, so it follows ALERTS=all and is never Time Sensitive.
        """
        if not self._wants(problem=False):
            return
        printer = printer_name or serial
        if job_name and not self._config.privacy_mode:
            text = f"{job_name} started on {printer}."
        else:
            text = f"A print started on {printer}."
        if eta_seconds and eta_seconds > 0:
            hours, minutes = divmod(round(eta_seconds / 60), 60)
            text += f" About {hours}h {minutes:02d}m to go." if hours else \
                f" About {minutes}m to go."
        await self._send(serial, "started", "Print started", text,
                         time_sensitive=False, dedupe_key=dedupe_key)

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

    async def hms_raised(self, serial: str, printer_name: Optional[str],
                         faults: "list") -> None:
        """One or more new HMS faults appeared.

        Batched into a single push rather than one per fault, because a single
        physical event routinely raises several codes at once and three
        notifications for one empty spool is how people turn alerts off.
        """
        # Gated by its OWN flag, so `ALERTS=problems` does not veto it. The
        # condition alerts are each an explicit request; only `ALERTS=off`,
        # which means "send me nothing", outranks that.
        if self._config.alert_hms == "off" or self._config.alerts == "off":
            return
        worst = min(faults, key=lambda e: e.get("severity", 4))
        described = [f for f in faults if f.get("description")]
        if described:
            lead = str(min(described, key=lambda e: e.get("severity", 4))["description"])
        else:
            # No catalogue entry. The code is all there is, and it is still
            # worth saying: it is what the user will search for.
            lead = f"Fault {worst.get('full_code', 'unknown')}"
        if len(faults) > 1:
            lead = f"{lead} (and {len(faults) - 1} more)"
        await self._send(
            serial, f"hms:{worst.get('full_code', '?')}",
            printer_name or serial, lead,
            # Severity 1 is fatal and 2 is serious; both are worth breaking
            # through Do Not Disturb for. 3 and 4 are not.
            time_sensitive=self._urgent(worst.get("severity", 4) <= 2),
        )

    async def hms_cleared(self, serial: str, printer_name: Optional[str]) -> None:
        """Every previously reported fault is gone.

        Never time-sensitive: good news that arrives quietly is still good
        news, and this exists mainly so a phone that buzzed at 3am shows the
        problem resolved without the user going to look.
        """
        # Gated by its OWN flag, so `ALERTS=problems` does not veto it. The
        # condition alerts are each an explicit request; only `ALERTS=off`,
        # which means "send me nothing", outranks that.
        if self._config.alert_hms == "off" or self._config.alerts == "off":
            return
        await self._send(serial, "hms-clear", printer_name or serial,
                         "Fault cleared", time_sensitive=False)

    async def printer_offline(self, serial: str, printer_name: Optional[str],
                              online: bool) -> None:
        """A printer dropped off BambuBuddy, or came back."""
        if not self._config.alert_offline or self._config.alerts == "off":
            return
        await self._send(
            serial, "offline" if not online else "online", printer_name or serial,
            "Back online" if online else "Printer went offline",
            time_sensitive=self._urgent(not online),
        )

    async def plate_clear(self, serial: str, printer_name: Optional[str]) -> None:
        """A finished plate is blocking the queue."""
        if not self._config.alert_plate_clear or self._config.alerts == "off":
            return
        await self._send(serial, "plate-clear", printer_name or serial,
                         "Waiting for the plate to be cleared", time_sensitive=False)

    async def print_paused(self, serial: str, printer_name: Optional[str],
                           reason: Optional[str], resumed: bool) -> None:
        """Paused, or resumed.

        A pause with a KNOWN cause is treated as a problem and a pause with no
        stated cause is not, because a deliberate pause is the user's own doing
        and an empty spool is not.
        """
        if not self._config.alert_pause or self._config.alerts == "off":
            return
        # A pause with a KNOWN cause is an incident; one the user made
        # themselves is not. Only the urgency differs, never whether it is
        # sent: ALERT_PAUSE was explicitly asked for, and under
        # `ALERTS=problems` the old gate silently dropped every resume and
        # every ordinary pause, forever.
        problem = bool(reason) and not resumed
        text = "Resumed" if resumed else (reason or "Paused")
        await self._send(serial, "resumed" if resumed else "paused",
                         printer_name or serial, text,
                         time_sensitive=self._urgent(problem))

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
