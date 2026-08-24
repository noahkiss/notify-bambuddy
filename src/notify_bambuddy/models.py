# ---------------------------------------------------------------------------
# The domain model.
#
# Two families of type live here:
#
#   1. THE INBOUND EVENT. A normalized, validated view of one status
#      response. Parsing happens once, at the edge (bambuddy_api.py), so that
#      nothing downstream ever touches a raw dict or has to remember that
#      `remaining_time` is in minutes.
#
#   2. SESSION STATE. What this service believes about one print on one
#      printer, and about the Live Activity tile representing it.
#
# The single most important thing in this file is `PrinterStatus.eta_seconds`.
# BambuBuddy reports `remaining_time` in MINUTES, taken straight from the
# printer firmware's `mc_remaining_time` field, while documentation elsewhere
# shows values next to a progress percentage that read unmistakably like
# seconds. They are not. Confirmed in BambuBuddy's own source, where a
# different consumer of the same field does
# `state.remaining_time * 60  # Convert minutes to seconds`
# in the printer client, next to the comment "Convert minutes to seconds".
# The status route does not
# convert; this service must. Getting it wrong turns a four hour print into a
# four minute countdown on the user's Lock Screen.
# ---------------------------------------------------------------------------

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse BambuBuddy's ISO-8601 timestamps into aware datetimes.

    Accepts `2026-01-13T12:00:00.000000+00:00`, the same value without its
    offset (assumed UTC), and a trailing `Z`. Python's
    `datetime.fromisoformat` did not learn `Z` until 3.11, and this project
    supports 3.9.

    Returns None rather than raising. A message with an unparseable timestamp
    is still a message; the caller substitutes its own arrival time and logs.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Printer states
#
# `state` on the status topic is a raw passthrough of the printer firmware's
# `gcode_state` string. BambuBuddy does not validate it and it is NOT a closed
# set: mid-print it can transiently take values like "LOADING" during AMS
# auto-fallback on a P2S, and it is the literal string "unknown" both before
# the first telemetry frame arrives and after BambuBuddy presumes a smart-plug
# Auto Off. So this is a list of the values worth RECOGNISING, never an
# exhaustive enum to switch on. Everything unrecognised is handled by holding
# the current tile unchanged, which is the only safe default.
# ---------------------------------------------------------------------------

STATE_RUNNING = "RUNNING"
STATE_PAUSE = "PAUSE"
STATE_PREPARE = "PREPARE"
STATE_SLICING = "SLICING"
STATE_FINISH = "FINISH"
STATE_FAILED = "FAILED"
STATE_IDLE = "IDLE"

# The literal BambuBuddy writes when it has no idea. Never a real printer
# state, and specifically NOT evidence that a print stopped.
STATE_UNKNOWN = "unknown"

# States in which a print is genuinely in flight. Note PAUSE is included: a
# paused print is an active print whose tile must stay on screen.
ACTIVE_STATES = frozenset({STATE_RUNNING, STATE_PAUSE})

# States that mean "no print is running right now". A terminal outcome is
# inferred from one of these persisting past INACTIVE_GRACE_SECONDS, which is
# the only way this service ever learns a print ended.
INACTIVE_STATES = frozenset({STATE_FINISH, STATE_FAILED, STATE_IDLE})


class Phase(enum.Enum):
    """What this service currently believes a print is doing.

    Deliberately coarser than the printer's own state. The tile only has a few
    distinguishable appearances, so the state machine only needs a few phases.
    """

    #: Not yet classified. Only the dataclass default: every real creation
    #: path sets the phase from what the printer is actually reporting, so
    #: that a tile does not appear saying "Starting" and correct itself to
    #: "Printing" a second later at the cost of a second push. Kept as a
    #: safety net for an unclassified session rather than removed, so the
    #: default can never render as something misleading.
    STARTING = "starting"

    #: Normal forward progress.
    PRINTING = "printing"

    #: `state == "PAUSE"`. The print is alive, the countdown is meaningless,
    #: and the user usually wants to know why.
    PAUSED = "paused"

    #: No telemetry for longer than the silence threshold. The print may still
    #: be running perfectly; the printer may simply have been switched off at
    #: the wall. Either way the numbers on the tile stopped being true, so the
    #: tile says so rather than freezing a lie at 96%.
    SILENT = "silent"

    #: A terminal event has been seen. The tile shows the outcome and is held
    #: briefly (see the terminal hold in coordinator.py) before being ended.
    TERMINAL = "terminal"


class Outcome(enum.Enum):
    """How a print ended.

    Derived from the printer's own state once it goes inactive: FINISH means
    completed, FAILED means failed, and anything else inactive is treated as
    aborted, because a print that simply stopped being reported is not a
    success and claiming otherwise would be the worse error.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    #: The user stopped the print through BambuBuddy's own UI. A stop from the
    #: printer's touchscreen arrives as ABORTED instead, because BambuBuddy
    #: only knows about stops it issued itself.
    ABORTED = "aborted"
    #: Not a BambuBuddy outcome. Synthesised locally when a genuinely new print
    #: displaces a session this service still had open.
    SUPERSEDED = "superseded"

    @property
    def is_success(self) -> bool:
        return self is Outcome.COMPLETED

    @property
    def rank(self) -> int:
        """Priority when one outcome would replace another.

        Higher wins. Success outranks everything, because a printer that
        reported FINISH did finish, and a user whose Lock Screen said
        "Cancelled" for a successful print has been actively misinformed.

        This matters because outcomes here are INFERRED rather than announced,
        so a first one can be wrong: a printer that drops off the network
        looks like one that stopped, and the tile must be able to correct
        itself when it comes back.
        """
        return {
            Outcome.SUPERSEDED: 0,
            Outcome.ABORTED: 1,
            Outcome.FAILED: 2,
            Outcome.COMPLETED: 3,
        }[self]
# ---------------------------------------------------------------------------
# Inbound events
#
# Every event carries `serial` and `timestamp`. The serial is the printer's
# identity: BambuBuddy's printer NAMES are user-editable and its numeric
# printer IDs are database rows that can be renumbered, but the serial is
# burned into the machine. The timestamp is the publisher's own clock, and it
# is the basis of both duplicate suppression and staleness detection, so it is
# parsed once here and never re-derived.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrinterStatus:
    """One reading of a printer's state.

    Built from a `GET /api/v1/webhook/printer/{id}/status` response.
    Everything downstream consumes this shape and never touches the raw dict.

    `timestamp` is stamped when the response ARRIVES, not read from the
    payload. That is not a shortcut: the response was generated to answer this
    request, so its age is the round trip and nothing else.
    """

    serial: str
    printer_id: Optional[int]
    printer_name: Optional[str]
    timestamp: datetime

    #: BambuBuddy's belief about its own connection to the printer. Treat a
    #: False here as "every other field is a last-seen value", never as a
    #: fresh reading. Note it can also stay True forever after a hard power
    #: cut, which is why the age of the reading is the primary liveness signal
    #: and this flag is only corroboration.
    connected: bool

    #: Raw `gcode_state`. See the STATE_* constants above; not a closed set.
    state: str

    #: Percent complete, 0-100. Mirrors the firmware's `mc_percent` verbatim,
    #: including its reset to 0 on cancel. It is NOT reset when a new print
    #: begins, so during PREPARE it still carries the PREVIOUS print's
    #: percentage. Never treat a nonzero value on a freshly started print as
    #: evidence of real progress.
    progress: float

    #: Minutes remaining, as reported by the firmware. 0 means "not known
    #: yet", not "finishing now": it reads 0 during pre-print calibration and
    #: BambuBuddy's own frontend treats <= 0 as unknown. Use `eta_seconds`.
    #:
    #: MINUTES, which is worth stating twice because getting it wrong is the
    #: highest-consequence mistake available here. BambuBuddy stores the
    #: firmware's `mc_remaining_time` verbatim and the status route passes it
    #: through untouched, while OTHER call sites inside BambuBuddy multiply by
    #: 60 before handing it to code that wants seconds. Reading it as seconds
    #: turns a two hour print into a two minute countdown.
    remaining_minutes: int

    layer_num: int
    total_layers: int

    #: The three name fields, in decreasing order of usefulness to a human.
    #: `subtask_name` is the display name and is absent for local/SD prints;
    #: `gcode_file` is the raw firmware path such as
    #: `/data/Metadata/plate_2.gcode`; `current_print` is a derived alias equal
    #: to subtask_name when set, else gcode_file, so it is never worth reading
    #: directly.
    subtask_name: Optional[str]
    gcode_file: Optional[str]
    current_print: Optional[str]

    #: BambuBuddy's own flag, not printer telemetry: raised on every terminal
    #: status and lowered when someone confirms the build plate is clear. It
    #: survives power cycles, which makes it durable evidence that the last
    #: print is over even when the retained status still claims RUNNING.
    awaiting_plate_clear: bool

    @property
    def eta_seconds(self) -> Optional[int]:
        """Remaining time in SECONDS, or None when not known.

        The minutes-to-seconds conversion the whole integration depends on.
        Returns None rather than 0 for the unknown case so that callers cannot
        accidentally send `endsIn: 0`, which Notify rejects with a 400 and
        which would mean no tile at all for the first frames of every print.
        """
        if self.remaining_minutes is None or self.remaining_minutes <= 0:
            return None
        return self.remaining_minutes * 60

    @property
    def is_active(self) -> bool:
        """True when the printer reports a print in flight (running or paused)."""
        return self.state in ACTIVE_STATES

    @property
    def is_unknown(self) -> bool:
        """True for BambuBuddy's placeholder state.

        Callers must treat this as "no information arrived", never as a
        transition. It appears immediately after BambuBuddy reconnects to a
        printer, before the first real telemetry frame answers.
        """
        return self.state == STATE_UNKNOWN or not self.state



#: The one event shape the poller produces. Kept as an alias because the
#: coordinator's entry point is typed loosely, and because a second source
#: would reintroduce a union here rather than change every signature.
InboundEvent = PrinterStatus

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


class Delivery(enum.Enum):
    """Where a session's tile is in its life with the Notify gateway."""

    #: No tile yet. Either the print was just detected, or a start is queued
    #: behind the device's 5-tile cap.
    PENDING = "pending"
    #: A start returned an activityId. The tile may not have appeared on the
    #: phone yet; that is confirmed separately by the verification poll.
    ACTIVE = "active"
    #: The gateway confirmed the tile is gone and this service must not
    #: recreate it for this print. Set only for a user dismissal or a dead
    #: token, never for a server-side sweep.
    DISMISSED = "dismissed"
    #: Terminal content has been written and the hold timer is running.
    ENDING = "ending"
    #: Fully finished. The row is kept briefly for diagnostics.
    ENDED = "ended"


@dataclass
class SessionState:
    """Everything this service knows about one print on one printer.

    One session per printer at a time, because a printer prints one thing at
    a time. That single fact is what makes correlating an ending tractable:
    nothing carries a print id, so the only sound way to decide which print
    just ended is "the open session for this printer IS the print that just
    ended". Matching on filename does not work, because the name reported as a
    print winds down is not always the name it started under.
    """

    #: Local primary key: `{serial}:{epoch_seconds_of_detection}`. Used only
    #: as this service's own identifier. It is deliberately NOT derived from
    #: anything BambuBuddy publishes, because nothing BambuBuddy publishes is
    #: both stable and always present.
    session_key: str

    serial: str
    printer_name: Optional[str] = None

    phase: Phase = Phase.STARTING
    delivery: Delivery = Delivery.PENDING

    #: Notify's handle for the tile, and Apple's hard 8-hour ceiling for it.
    activity_id: Optional[str] = None
    expires_at: Optional[datetime] = None

    #: Display fields, carried forward so an update can be built even from a
    #: status frame that omits something.
    job_name: Optional[str] = None
    progress: float = 0.0
    eta_seconds: Optional[int] = None
    layer_num: int = 0
    total_layers: int = 0

    #: Set when a terminal event arrives. The tile is NOT ended immediately:
    #: `terminal_at` starts a hold during which a contradicting, higher-ranked
    #: outcome can still correct the content.
    outcome: Optional[Outcome] = None
    terminal_at: Optional[float] = None

    #: The `job_id` from `queue/job_started`, when this print came from the
    #: queue. Purely informational; nothing depends on it being present.
    job_id: Optional[int] = None

    #: Newest source timestamp applied to this session, as an epoch float.
    #: Readings older than this are dropped rather than applied. A response
    #: delayed in flight can arrive after a newer one, and applying it would
    #: rewind the tile.
    last_source_ts: float = 0.0

    #: Monotonic clocks for pacing. Separate from source timestamps because
    #: they must not be affected by a publisher whose clock is wrong.
    created_at: float = 0.0
    last_sent_at: float = 0.0
    last_progress_sent_at: float = 0.0

    #: Hash of the last payload actually sent. Suppresses no-op updates. The
    #: keepalive deliberately bypasses it: the keepalive exists to refresh the
    #: gateway's freshness clock, and content that has not changed is exactly
    #: the case where it is needed most.
    last_payload_hash: Optional[str] = None

    #: What the tile MEANT as of the last push, as opposed to what it said.
    #: A change here always earns a push, bypassing the progress pacing floor,
    #: because these are the changes a user notices:
    #:
    #:   the phase          printing -> paused -> connection lost -> done
    #:   the status word    including "Paused" becoming "Filament out"
    #:   whether a countdown is on screen at all
    #:
    #: The last of those is easy to miss and matters most right after a tile
    #: is created: the first frames of a print often carry no ETA, so the tile
    #: starts without a countdown and the moment the printer works one out is
    #: exactly the update that must not sit behind a fifteen-second rate limit
    #: designed for percentage ticks.
    #:
    #: Runtime only, not persisted. After a restart it is None, which forces
    #: one push per session, which is the right behaviour there too.
    last_sent_signature: Optional[tuple] = None

    #: True once progress can be believed. BambuBuddy never resets `progress`
    #: at print start, so a new print's first frames carry the previous
    #: print's percentage. Progress is pinned to 0 until either a layer number
    #: appears or the reported percentage drops below where it started.
    progress_trustworthy: bool = False
    #: The percentage observed at detection, used to spot that drop.
    progress_at_detection: float = 0.0


    #: Whether the terminal push notification for this print has gone out.
    #: A success alerts at once; other outcomes alert when the hold expires,
    #: and this is what stops the end path sending a second one. PERSISTED:
    #: a restart inside the hold would otherwise send it again.
    alert_sent: bool = False

    #: Whether the terminal outcome was INFERRED (silence, or an inactive
    #: state with no event) rather than reported. PERSISTED, so a restart
    #: inside the hold does not turn a guess into a confident alert. An inferred "stopped" is
    #: not worth a "Print stopped" notification: the silence alert already
    #: said what is known, and claiming the print stopped would be a guess.
    outcome_inferred: bool = False

    #: The countdown value last written to the tile, and the source time it
    #: was written at. `endsIn` is relative, so the gateway recomputes the
    #: finish from "now" every time it arrives; resending an unchanged value
    #: therefore pushes the finish later. These two are what make the resend
    #: conditional on the estimate having actually moved.
    sent_eta_seconds: Optional[int] = None
    sent_eta_at: float = 0.0

    #: True when the tile answered 410 but the reason could not be read. The
    #: tile is gone either way; what is unknown is whether the USER made it go,
    #: which decides whether another may be created. Retried rather than
    #: guessed. Runtime only: after a restart the session is reconciled
    #: against the device list anyway, which answers the same question.
    pending_gone: bool = False

    #: The linger time an outstanding end still owes, remembered across a
    #: retry so a redelivered DELETE says the same thing the first one did.
    end_keep_for: int = 0

    #: True when the terminal outcome was reported while the printer was still
    #: saying RUNNING, which is the signature of BambuBuddy's restart
    #: reconciliation rather than a real ending. Such an outcome is held but
    #: never ended on a timer, and a later RUNNING frame revokes it outright.
    terminal_provisional: bool = False

    #: Set BEFORE a start request is sent, and cleared once the gateway has
    #: answered one way or the other. If it survives to the next run, the
    #: process died mid-start and the gateway may hold a tile nobody owns, so
    #: recovery reconciles instead of starting another. PERSISTED: this is the
    #: whole point of it.
    #: How many tiles this ONE print has been given. Bounds the recreate
    #: loop: a gateway that reaps every tile as never-started would otherwise
    #: have a new one started for it forever, and every attempt is a start the
    #: device's own throttle counts.
    tile_attempts: int = 0

    start_ambiguous: bool = False

    #: Epoch seconds of the last status frame, for silence detection.
    last_status_ts: float = 0.0

    def is_live(self) -> bool:
        """True while the tile should still be receiving updates."""
        return self.delivery in (Delivery.PENDING, Delivery.ACTIVE)
