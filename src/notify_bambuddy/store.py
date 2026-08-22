# ---------------------------------------------------------------------------
# Durable state.
#
# Three things have to survive a restart, and only three:
#
#   1. WHICH TILE BELONGS TO WHICH PRINT. Without this, a restart mid-print
#      orphans the tile: it keeps sitting on the Lock Screen, nothing updates
#      it, and the gateway eventually reaps it hours later. With it, the
#      service reattaches and carries on mid-countdown.
#
#   2. WHICH TILES THE USER SWIPED AWAY. Dismissal has to be final across
#      restarts, or the service becomes a machine for putting a tile back on
#      the Lock Screen of someone who just took it off. This matters more here
#      than in most integrations, because starting tiles with `new=1` (which
#      multi-printer support requires) deliberately bypasses the gateway's own
#      dismissal protection. Honouring dismissal is therefore entirely this
#      service's job.
#
# SQLite in WAL mode, on a volume the user is told to make persistent. Writes
# are small and infrequent, so they run inline rather than through a thread
# pool: at this volume the round trip through an executor would cost more than
# the write itself.
#
# Only the event loop thread reaches this class today, but every method still
# holds a lock across its statement AND its commit. A shared sqlite3
# connection shares transaction state and a connection-level change counter,
# so the day anything else touches it, interleaving either one produces
# "cannot commit transaction" or, worse, a silently wrong rowcount. The lock
# costs tens of microseconds and removes a whole class of future accident.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from .models import Delivery, Outcome, Phase, SessionState

log = logging.getLogger(__name__)

#: How long a dismissal suppresses new tiles for the SAME print. A genuinely
#: new print clears it immediately (see `clear_dismissal`), and so does that
#: print reaching a terminal state; this ceiling only exists so a dismissal
#: cannot outlive any plausible print if BOTH those signals are missed.
#:
#: Long enough to cover a print that outlives Apple's eight-hour tile ceiling
#: and rolls over: 3D prints of two or three days are ordinary, and a
#: dismissal that lapsed mid-print would put back a tile the user had already
#: refused once.
DISMISSAL_TTL_SECONDS = 72 * 3600

#: Finished sessions are kept this long purely for the health endpoint and
#: for debugging a "what happened to my tile" question.
ENDED_RETENTION_SECONDS = 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key           TEXT PRIMARY KEY,
    serial                TEXT NOT NULL,
    printer_name          TEXT,
    phase                 TEXT NOT NULL,
    delivery              TEXT NOT NULL,
    activity_id           TEXT,
    expires_at            REAL,
    job_name              TEXT,
    job_id                INTEGER,
    progress              REAL NOT NULL DEFAULT 0,
    eta_seconds           INTEGER,
    layer_num             INTEGER NOT NULL DEFAULT 0,
    total_layers          INTEGER NOT NULL DEFAULT 0,
    outcome               TEXT,
    terminal_at           REAL,
    last_source_ts        REAL NOT NULL DEFAULT 0,
    created_at            REAL NOT NULL DEFAULT 0,
    last_sent_at          REAL NOT NULL DEFAULT 0,
    last_payload_hash     TEXT,
    progress_trustworthy  INTEGER NOT NULL DEFAULT 0,
    progress_at_detection REAL NOT NULL DEFAULT 0,
    last_status_ts        REAL NOT NULL DEFAULT 0,
    updated_at            REAL NOT NULL DEFAULT 0,
    -- These four decide what happens AFTER a restart, so they are the last
    -- things that may be left in memory:
    --   start_ambiguous     a start was in flight when we stopped, so the
    --                       gateway may hold a tile we have to reconcile
    --   alert_sent          the terminal notification already went out
    --   outcome_inferred    the outcome was guessed, not reported
    --   terminal_provisional a failure reported while the printer still ran
    start_ambiguous       INTEGER NOT NULL DEFAULT 0,
    alert_sent            INTEGER NOT NULL DEFAULT 0,
    outcome_inferred      INTEGER NOT NULL DEFAULT 0,
    terminal_provisional  INTEGER NOT NULL DEFAULT 0,
    end_keep_for          INTEGER NOT NULL DEFAULT 0
);

-- One index, on the lookup the coordinator does constantly: "is there an open
-- session for this printer?"
CREATE INDEX IF NOT EXISTS sessions_by_serial ON sessions (serial, delivery);

-- Keyed by PRINTER, not by session. A session key is derived from the moment
-- a print was detected, so it cannot be reconstructed after a restart, and a
-- dismissal recorded against one would be silently forgotten the moment this
-- service was upgraded. Keying on the serial and clearing the row when a
-- genuinely new print starts gives the same "do not recreate this print's
-- tile" behaviour and survives restarts, which is the case that matters.
CREATE TABLE IF NOT EXISTS dismissals (
    serial       TEXT PRIMARY KEY,
    dismissed_at REAL NOT NULL,
    -- Which print the swipe was made against. A dismissal means "not this
    -- print", so without this there is nothing that can ever decide the print
    -- has changed, and a swipe followed by a restart silences the printer for
    -- the whole TTL. NULL on rows written before this column existed.
    job          TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """The service's durable state, in one SQLite file."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                try:
                    os.makedirs(directory, exist_ok=True)
                except OSError as exc:
                    # Worth catching rather than letting the raw errno out: the
                    # usual cause is a STATE_DB pointing somewhere this account
                    # cannot write, and the fix is one environment variable.
                    raise OSError(
                        f"Cannot create the state directory {directory!r}: {exc}. "
                        "Set STATE_DB to a path this user can write, for example "
                        "STATE_DB=./notify-bambuddy.db"
                    ) from exc
        # `check_same_thread=False` plus a lock, rather than either alone.
        #
        # Sharing a connection across threads means sharing its transaction
        # state and its change counter, so two threads interleaving
        # `execute()` and `commit()` produce "cannot commit transaction - SQL
        # statements in progress", and `cursor.rowcount` reads a CONNECTION
        # level counter, which can be wrong with no error at all.
        #
        # The lock makes each statement-plus-commit atomic, which is the
        # actual requirement. It is uncontended in practice and is far safer
        # than a second connection, which would turn these into real lock
        # contention against the `busy_timeout`.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._configure()
            self._db.executescript(_SCHEMA)
            self._migrate()
            self._db.commit()

    #: Columns added after the first schema existed, per table. `CREATE TABLE
    #: IF NOT EXISTS` does nothing to a table that is already there, so a
    #: database written by an older build needs them added explicitly.
    #:
    #: Keyed by table because forgetting that is a live-upgrade crash: a
    #: column added only to the CREATE statement exists for a fresh install
    #: and is missing for everyone who already had the service running, which
    #: is the exact population an upgrade is for.
    _ADDED_COLUMNS = {
        "sessions": (
            ("start_ambiguous", "INTEGER NOT NULL DEFAULT 0"),
            ("alert_sent", "INTEGER NOT NULL DEFAULT 0"),
            ("outcome_inferred", "INTEGER NOT NULL DEFAULT 0"),
            ("terminal_provisional", "INTEGER NOT NULL DEFAULT 0"),
            ("end_keep_for", "INTEGER NOT NULL DEFAULT 0"),
        ),
        # Nullable with no default: an existing dismissal genuinely does not
        # know which print it belonged to, and NULL says so honestly. The
        # reader treats an unknown job as "cannot tell", which keeps the old
        # conservative behaviour for rows written before this column existed.
        "dismissals": (
            ("job", "TEXT"),
        ),
    }

    def _migrate(self) -> None:
        """Add any column this build expects that an older database lacks.

        Deliberately additive and idempotent. There is no version number and
        no down-migration: every column has a default that means "the old
        behaviour", so an older database simply reads as a service that had
        none of these flags set, which is exactly what it was.
        """
        for table, columns in self._ADDED_COLUMNS.items():
            existing = {
                row[1] for row in
                self._db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, spec in columns:
                if column not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
                    log.info("Added the %s column to %s in the state store", column, table)

    def _configure(self) -> None:
        # WAL: a reader (the health endpoint) never blocks the writer, and an
        # unclean shutdown, which for a container means every `docker stop`
        # that overruns its grace period, recovers without losing committed
        # state.
        self._db.execute("PRAGMA journal_mode=WAL")
        # NORMAL rather than FULL: this is a cache of derived state, not a
        # ledger. Losing the last few milliseconds of writes in a power cut
        # costs at most one redundant tile update on the next start, and FULL
        # would fsync on every progress tick.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        with self._lock:
            try:
                self._db.commit()
            finally:
                self._db.close()

    # --- Sessions ----------------------------------------------------------

    def save_session(self, session: SessionState) -> None:
        """Insert or replace one session row."""
        with self._lock:
            self._save_session_locked(session)

    def _save_session_locked(self, session: SessionState) -> None:
        self._db.execute(
            """
            INSERT INTO sessions (
                session_key, serial, printer_name, phase, delivery, activity_id, expires_at,
                job_name, job_id, progress, eta_seconds, layer_num, total_layers, outcome,
                terminal_at, last_source_ts, created_at, last_sent_at, last_payload_hash,
                progress_trustworthy, progress_at_detection, last_status_ts, updated_at,
                start_ambiguous, alert_sent, outcome_inferred, terminal_provisional,
                end_keep_for
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_key) DO UPDATE SET
                printer_name=excluded.printer_name,
                phase=excluded.phase,
                delivery=excluded.delivery,
                activity_id=excluded.activity_id,
                expires_at=excluded.expires_at,
                job_name=excluded.job_name,
                job_id=excluded.job_id,
                progress=excluded.progress,
                eta_seconds=excluded.eta_seconds,
                layer_num=excluded.layer_num,
                total_layers=excluded.total_layers,
                outcome=excluded.outcome,
                terminal_at=excluded.terminal_at,
                last_source_ts=excluded.last_source_ts,
                last_sent_at=excluded.last_sent_at,
                last_payload_hash=excluded.last_payload_hash,
                progress_trustworthy=excluded.progress_trustworthy,
                progress_at_detection=excluded.progress_at_detection,
                last_status_ts=excluded.last_status_ts,
                updated_at=excluded.updated_at,
                start_ambiguous=excluded.start_ambiguous,
                alert_sent=excluded.alert_sent,
                outcome_inferred=excluded.outcome_inferred,
                terminal_provisional=excluded.terminal_provisional,
                end_keep_for=excluded.end_keep_for
            """,
            (
                session.session_key,
                session.serial,
                session.printer_name,
                session.phase.value,
                session.delivery.value,
                session.activity_id,
                session.expires_at.timestamp() if session.expires_at else None,
                session.job_name,
                session.job_id,
                float(session.progress),
                session.eta_seconds,
                session.layer_num,
                session.total_layers,
                session.outcome.value if session.outcome else None,
                session.terminal_at,
                session.last_source_ts,
                session.created_at,
                session.last_sent_at,
                session.last_payload_hash,
                1 if session.progress_trustworthy else 0,
                session.progress_at_detection,
                session.last_status_ts,
                time.time(),
                1 if session.start_ambiguous else 0,
                1 if session.alert_sent else 0,
                1 if session.outcome_inferred else 0,
                1 if session.terminal_provisional else 0,
                session.end_keep_for,
            ),
        )
        self._db.commit()

    def load_live_sessions(self) -> list[SessionState]:
        """Every session that was still in flight when the process stopped.

        These are the tiles this service may still own on the user's phone.
        The coordinator reconciles each one against the gateway before
        touching it, because a tile can have been dismissed, reaped, or simply
        expired while this service was down.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE delivery IN (?,?,?) ORDER BY created_at",
                (Delivery.PENDING.value, Delivery.ACTIVE.value, Delivery.ENDING.value),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def get_session(self, session_key: str) -> Optional[SessionState]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> SessionState:
        from datetime import datetime, timezone

        expires_at = row["expires_at"]
        return SessionState(
            session_key=row["session_key"],
            serial=row["serial"],
            printer_name=row["printer_name"],
            phase=Phase(row["phase"]),
            delivery=Delivery(row["delivery"]),
            activity_id=row["activity_id"],
            expires_at=(
                datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at else None
            ),
            job_name=row["job_name"],
            job_id=row["job_id"],
            progress=row["progress"],
            eta_seconds=row["eta_seconds"],
            layer_num=row["layer_num"],
            total_layers=row["total_layers"],
            outcome=Outcome(row["outcome"]) if row["outcome"] else None,
            terminal_at=row["terminal_at"],
            last_source_ts=row["last_source_ts"],
            created_at=row["created_at"],
            last_sent_at=row["last_sent_at"],
            last_payload_hash=row["last_payload_hash"],
            progress_trustworthy=bool(row["progress_trustworthy"]),
            progress_at_detection=row["progress_at_detection"],
            last_status_ts=row["last_status_ts"],
            start_ambiguous=bool(row["start_ambiguous"]),
            alert_sent=bool(row["alert_sent"]),
            outcome_inferred=bool(row["outcome_inferred"]),
            terminal_provisional=bool(row["terminal_provisional"]),
            end_keep_for=row["end_keep_for"],
        )

    # --- Dismissals --------------------------------------------------------

    def record_dismissal(self, serial: str, job: Optional[str] = None) -> None:
        """Remember that the user swiped away this printer's current tile.

        Scoped to the CURRENT PRINT, expressed as "this printer, until a new
        print starts". Suppressing the printer forever would mean one
        impatient swipe silences a machine for good; suppressing nothing would
        mean the tile reappears within seconds, which is worse. The row is
        cleared by `clear_dismissal` the moment a genuinely new print is
        detected.

        Honouring dismissal is entirely this service's job, not the gateway's.
        Multi-printer support requires starting tiles with `new=1`, and that
        flag deliberately skips the gateway's own sticky-dismissal check.
        """
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO dismissals (serial, dismissed_at, job) "
                "VALUES (?,?,?)",
                (serial, time.time(), job),
            )
            self._db.commit()

    def dismissed_job(self, serial: str) -> Optional[str]:
        """The print a live dismissal was made against, if it is known.

        None means either no dismissal or one recorded before dismissals
        carried a job name. Both read as "cannot tell", and the caller keeps
        the conservative behaviour rather than guessing the print has changed.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT job FROM dismissals WHERE serial = ?", (serial,)
            ).fetchone()
        return row["job"] if row else None

    def is_dismissed(self, serial: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT dismissed_at FROM dismissals WHERE serial = ?", (serial,)
            ).fetchone()
        if not row:
            return False
        if time.time() - row["dismissed_at"] > DISMISSAL_TTL_SECONDS:
            return False
        return True

    def clear_dismissal(self, serial: str) -> None:
        """Forget a printer's dismissal, because a new print started.

        Called when a printer that had no live print starts reporting one
        again. Without it a single swipe would silence that printer for the
        full dismissal TTL.
        """
        with self._lock:
            self._db.execute("DELETE FROM dismissals WHERE serial = ?", (serial,))
            self._db.commit()

    # --- Maintenance -------------------------------------------------------

    def prune(self) -> None:
        """Drop rows nothing will read again. Cheap; called on a slow timer."""
        now = time.time()
        with self._lock:
            self._db.execute(
                "DELETE FROM dismissals WHERE dismissed_at < ?", (now - DISMISSAL_TTL_SECONDS,)
            )
            self._db.execute(
                "DELETE FROM sessions WHERE delivery IN (?,?) AND updated_at < ?",
                (Delivery.ENDED.value, Delivery.DISMISSED.value, now - ENDED_RETENTION_SECONDS),
            )
            self._db.commit()

    # --- Small key/value ---------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )
            self._db.commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?",
                                   (key,)).fetchone()
        return row["value"] if row else None
