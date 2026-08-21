# ---------------------------------------------------------------------------
# Turning a print into tile content.
#
# The tile has fewer slots than it looks like it has, and that shapes every
# decision in this module. The widget renders ONE thing in its trailing
# position, in a fixed priority order:
#
#       live countdown  >  trailing text  >  status word
#
# So a tile sent `endsIn` AND `trailing: "Layer 412/1247"` AND
# `status: "Printing"` shows the countdown and nothing else. The layer counter
# and the phase word are simply invisible, on every surface, forever. Sending
# them anyway costs an HTTP round trip and an APNs push per update to deliver
# two fields that cannot render.
#
# The answer is to let the PHASE decide which ONE thing occupies the slot:
#
#   printing, ETA known   -> countdown. The most useful thing a 3D printing
#                            tile can show, and it costs one request for its
#                            whole life because the device ticks it locally.
#   printing, no ETA yet  -> layer counter, until the printer works out an ETA.
#   paused                -> the word "Paused" or "Filament out". NOT the layer
#                            counter: `trailing` outranks `status` on the
#                            widget, so sending both would show the layer count
#                            and hide the one word the user needs to see.
#   silent                -> "Connection lost", for the same reason.
#   finished              -> the outcome word.
#
# `status` is sent in every phase regardless, because it is free and it is the
# widget's fallback: it becomes visible the moment a countdown lapses, which
# covers the very common case of a printer overrunning its own ETA. Whatever
# must NOT be on screen in a phase is explicitly cleared with JSON null rather
# than merely omitted, because an omitted field keeps its previous value.
#
# CLEARING THE COUNTDOWN HAS A COST, and it is worth stating plainly because
# it creates a dependency between this file and the keepalive in
# coordinator.py. Sending `endsIn: null` deletes the tile's end time, which
# turns it into a progress-only tile, and the gateway's housekeeping ends any
# progress-only tile that has gone 2 hours without an update. This is safe
# ONLY because the keepalive fires unconditionally every 20 minutes and
# deliberately ignores the redundant-payload check. If that keepalive is ever
# made conditional, a print paused overnight will lose its tile.
# ---------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Optional

from .config import Config
from .models import Outcome, Phase, SessionState

# --- Hard limits from the Notify API ---------------------------------------
# Over-length strings are rejected with a 400 rather than truncated, so
# everything is trimmed here first. The one field that clamps instead of
# rejecting is `progress`, which the gateway pins to 0-100 at merge time; it
# is clamped here anyway so the value stored locally matches what was sent.
MAX_TITLE = 120
MAX_BODY = 300
MAX_STATUS = 40
MAX_TRAILING = 40

#: `endsIn` is 1 to 86400 seconds. Both ends bite in practice. Zero arrives
#: constantly, because the printer reports no ETA during pre-print
#: calibration and BambuBuddy's field defaults to 0 rather than null; sending
#: `endsIn: 0` is a 400, and on a START a 400 means no tile at all for that
#: print. The ceiling bites on long multi-plate jobs: a 30-hour plate is
#: 108000 seconds, and the gateway rejects it with a message about
#: milliseconds, which is precisely the wrong debugging hint when the units
#: were already right.
MAX_ENDS_IN = 86400
MIN_ENDS_IN = 1

#: Filenames the firmware generates that carry no information at all. A
#: multi-plate project slices to `/data/Metadata/plate_2.gcode`, and a tile
#: reading "plate_2" tells the user strictly less than showing nothing.
_MEANINGLESS_NAME = re.compile(r"^plate[_\-]?\d+$", re.IGNORECASE)

#: Extensions to strip from a filename, longest first so `.gcode.3mf` is
#: removed whole rather than leaving a stray `.gcode`.
_EXTENSIONS = (".gcode.3mf", ".gcode", ".3mf", ".stl")


def job_display_name(subtask_name: Optional[str], gcode_file: Optional[str]) -> Optional[str]:
    """Pick the most human name available for a print, or None.

    Three name fields arrive on the status topic and only one of them is meant
    for people:

      subtask_name   the display name. Best, but absent for prints started
                     from an SD card or the printer's own screen.
      gcode_file     the raw firmware path, e.g. `/data/Metadata/plate_2.gcode`.
      current_print  a derived alias, equal to subtask_name when that is set
                     and gcode_file otherwise, so reading it directly only
                     obscures which of the two was used.

    Returning None is a real answer, and better than a bad name: the tile's
    body is optional, and an empty second line looks intentional in a way that
    "plate_2" does not.
    """
    if subtask_name and subtask_name.strip():
        return subtask_name.strip()

    if not gcode_file or not gcode_file.strip():
        return None

    # The firmware uses forward slashes regardless of platform, but
    # `os.path.basename` on Windows would also split on a backslash, which is
    # harmless here and slightly more forgiving.
    base = os.path.basename(gcode_file.strip().replace("\\", "/"))
    for extension in _EXTENSIONS:
        if base.lower().endswith(extension):
            base = base[: -len(extension)]
            break

    base = base.strip()
    if not base or _MEANINGLESS_NAME.match(base):
        return None
    return base


def status_word(session: SessionState) -> str:
    """The short phase word shown when no countdown is on screen."""
    if session.phase is Phase.TERMINAL and session.outcome is not None:
        return {
            Outcome.COMPLETED: "Done",
            Outcome.FAILED: "Failed",
            Outcome.CANCELLED: "Cancelled",
            Outcome.ABORTED: "Stopped",
            Outcome.SUPERSEDED: "Replaced",
        }[session.outcome]
    if session.phase is Phase.PAUSED:
        # The distinction worth waking someone up for. A deliberate pause can
        # wait until morning; an empty spool cannot.
        return "Paused"
    if session.phase is Phase.SILENT:
        return "Connection lost"
    if session.phase is Phase.STARTING:
        return "Starting"
    return "Printing"


def _clamp_progress(value: float) -> int:
    """Percent as a whole number in 0-100.

    Rounded down rather than nearest, so a tile never reads 100% before the
    print is actually finished. Seeing 100% next to a still-running countdown
    is the kind of small wrongness that makes a user distrust the whole tile.
    """
    if value is None:
        return 0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric != numeric:  # NaN, which the gateway would reject outright
        return 0
    return int(max(0.0, min(100.0, numeric)))


def _clamp_ends_in(seconds: Optional[int]) -> Optional[int]:
    """Fit an ETA into the API's accepted range, or drop it entirely."""
    if seconds is None:
        return None
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    if value < MIN_ENDS_IN:
        # Deliberately not floored to 1. A one-second countdown expires
        # immediately and then arms the gateway's `overdue` sweep, which ends
        # the tile 30 minutes later for a print that is still running.
        return None
    return min(value, MAX_ENDS_IN)


#: Characters that have no business on a Lock Screen. NUL is rejected by the
#: gateway outright; the rest of the C0/C1 control range and the line and
#: paragraph separators either render as nothing or break the line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _utf16_len(text: str) -> int:
    """Length the way the gateway measures it.

    JavaScript's `string.length` counts UTF-16 code units, so a name full of
    emoji or CJK is longer to the gateway than `len()` says it is here, and
    trimming by Python code points would still get a 400 back.
    """
    return len(text.encode("utf-16-le")) // 2


def _trim(value: Optional[str], limit: int) -> Optional[str]:
    """Trim a string to the API's limit, with an ellipsis when it was cut."""
    if value is None:
        return None
    text = _CONTROL_CHARS.sub(" ", value)
    text = " ".join(text.split())  # collapse newlines and runs of whitespace
    if not text:
        return None
    if _utf16_len(text) <= limit:
        return text
    # Walk back until it fits in UTF-16 units with room for the ellipsis.
    cut = text
    while cut and _utf16_len(cut) > limit - 1:
        cut = cut[:-1]
    return cut.rstrip() + "…"


def layer_text(session: SessionState) -> Optional[str]:
    """`Layer 412/1247`, or None when the numbers cannot be trusted.

    Both halves go wrong in ordinary use. `total_layers` is only updated from
    positive values, because the firmware resets it to 0 at print end and
    BambuBuddy deliberately keeps the last good denominator rather than
    showing a zero; the consequence is that at the start of a NEW print it can
    still hold the PREVIOUS print's total. `layer_num` is reset to 0 at print
    start, so the pair can transiently read `3/1247` from two different prints.

    Both suspicious cases are dropped rather than shown. An absent layer
    counter is unremarkable; a wrong one is a bug report.
    """
    if session.total_layers <= 0 or session.layer_num <= 0:
        return None
    if session.layer_num > session.total_layers:
        return None
    return f"Layer {session.layer_num}/{session.total_layers}"


#: How far the printer's own estimate must move before the countdown is
#: rewritten. Below this the tile is already showing something within a
#: minute of the truth, and rewriting it would only push the finish time out.
ETA_RESEND_TOLERANCE_S = 90


def _countdown_worth_sending(session: SessionState, countdown: int) -> bool:
    """Whether to (re)send `endsIn` at all.

    Always on a tile that has no countdown yet. Otherwise only when the
    printer's estimate has moved further than the tolerance, allowing for the
    fact that a countdown already on screen has been ticking down on its own
    since it was sent.
    """
    if session.sent_eta_seconds is None:
        return True
    elapsed = max(0.0, session.last_status_ts - session.sent_eta_at)
    on_screen = session.sent_eta_seconds - elapsed
    return abs(countdown - on_screen) > ETA_RESEND_TOLERANCE_S


def commit_sent_eta(session: SessionState, content: dict) -> None:
    """Record what a payload the gateway ACCEPTED did to the tile's countdown.

    Deliberately separate from building. `build_content` is called
    speculatively all over the coordinator: to compute a hash, to compare
    significance, to derive a title for orphan matching, and for payloads that
    are then dropped by the pacing floor, by a gateway backoff, or by a failed
    request. If building recorded the countdown as sent, every one of those
    would leave the session believing a value reached the tile that never did.

    That failure is invisible and permanent. `_countdown_worth_sending` decays
    the recorded value by wall clock, and the printer's own estimate decays at
    the same rate, so a phantom record agrees with reality forever: the
    tolerance is never exceeded again and the countdown is never resent. The
    tile is stranded at whatever it last actually showed, which after a failed
    start is nothing at all.
    """
    if "endsIn" not in content:
        return          # untouched by this payload
    if content["endsIn"] is None:
        session.sent_eta_seconds = None
    else:
        session.sent_eta_seconds = content["endsIn"]
        session.sent_eta_at = session.last_status_ts


def countdown_after(session: SessionState, content: dict) -> bool:
    """Whether a countdown would be on the tile once `content` is applied.

    Not the same as whether `endsIn` is in the payload: an unchanged estimate
    is deliberately omitted, which leaves the existing countdown alone.
    """
    if "endsIn" not in content:
        return session.sent_eta_seconds is not None
    return content["endsIn"] is not None


def build_content(session: SessionState, config: Config, *, initial: bool) -> dict:
    """Build the JSON body for a start or an update.

    `initial=True` builds a complete tile for a start. `initial=False` builds
    a partial update, where an absent key means "leave this alone" and an
    explicit `None` (JSON null) means "clear this".

    Three fields are ever explicitly cleared, each because leaving the old
    value would be worse than an empty one: `endsIn` when the phase says a
    countdown would be a lie, `trailing` when something must not outrank the
    status word, and `body` under privacy mode.
    """
    content: dict[str, Any] = {}

    # --- Identity ---------------------------------------------------------
    # Title is the printer, not the model, because the tile's job is to answer
    # "which of my machines is this?" at a glance. The model name goes in the
    # body, where there is room for it.
    title = _trim(session.printer_name or session.serial, MAX_TITLE)
    if initial:
        content["title"] = title or "Printer"
        content["symbol"] = config.symbol
        content["tint"] = config.tint
    elif title:
        # A printer rename in BambuBuddy shows up on the next status frame;
        # following it costs nothing.
        content["title"] = title

    # --- What is printing -------------------------------------------------
    if config.privacy_mode:
        # Cleared, not omitted. Turning privacy mode on and restarting must
        # take the model name OFF a tile that is already showing it, and an
        # absent field leaves whatever was there.
        content["body"] = None
    else:
        body = _trim(session.job_name, MAX_BODY)
        if body:
            content["body"] = body

    # --- Progress ---------------------------------------------------------
    # Pinned to 0 until progress can be believed. BambuBuddy never resets its
    # progress field when a print starts, so the first frames of a new print
    # carry the last print's percentage: without this pin, a fresh print's
    # tile appears at 97% and then jumps backwards.
    content["progress"] = _clamp_progress(session.progress if session.progress_trustworthy else 0.0)

    # --- The trailing slot, which holds exactly one thing ------------------
    # Priority on the widget is countdown > trailing > status, so every phase
    # below sends at most one of the first two and clears the other. Clearing
    # is explicit (JSON null): an omitted field keeps whatever it said before,
    # and a stale layer counter left in `trailing` would hide the status word.
    countdown = _clamp_ends_in(session.eta_seconds)
    printing = session.phase in (Phase.PRINTING, Phase.STARTING)

    if printing and countdown is not None:
        # The countdown. When it lapses the widget falls through to `status`,
        # which reads "Printing": exactly right for an overrunning ETA.
        #
        # `endsIn` is RELATIVE: the gateway computes `endsAt = now + endsIn`
        # every time it is sent. So resending the same minute value a few
        # seconds later silently pushes the finish time out, and a tile
        # updated every fifteen seconds would drift later all print long
        # while never appearing to change. It is therefore sent only when the
        # printer's own estimate has actually moved, or when the tile has no
        # countdown yet.
        if _countdown_worth_sending(session, countdown):
            content["endsIn"] = countdown
        content["trailing"] = None
    elif printing:
        # No ETA yet. The layer counter is the most useful thing available
        # and "Printing" adds nothing it does not already imply.
        content["endsIn"] = None
        layers = layer_text(session)
        content["trailing"] = _trim(layers, MAX_TRAILING) if layers else None
    else:
        # Paused, silent, or finished. The one word that says so is the whole
        # point of the tile in these phases, and it only renders if nothing
        # outranks it. See the keepalive dependency in the module header for
        # why clearing `endsIn` here is safe.
        content["endsIn"] = None
        content["trailing"] = None

    # --- The phase word ----------------------------------------------------
    # Always sent. It is the fallback the widget reaches for when a countdown
    # expires, so keeping it current covers the overrunning-ETA case for free.
    content["status"] = _trim(status_word(session), MAX_STATUS)

    return content


def terminal_content(session: SessionState, config: Config) -> dict:
    """The last content a tile shows before it ends.

    Progress is forced to 100 only for a success. A print that failed at 40%
    should say 40%: the bar is a record of what happened, and rounding a
    failure up to a full bar erases the one detail that makes the tile worth
    looking at afterwards.
    """
    content = build_content(session, config, initial=False)
    if session.outcome is Outcome.COMPLETED:
        content["progress"] = 100
    # A finished print has no countdown, whatever it said a moment ago.
    content["endsIn"] = None
    return content


def payload_hash(content: dict) -> str:
    """A stable fingerprint of a payload, for suppressing no-op updates.

    Keys are sorted so that dict ordering never produces a spurious change.
    Only used to skip redundant sends; the keepalive ignores it entirely,
    because an unchanged tile is exactly the case where the gateway needs to
    hear from us anyway.
    """
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
