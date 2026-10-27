# ---------------------------------------------------------------------------
# Entry point: wire everything together and run.
#
# The startup order matters and is not arbitrary:
#
#   1. Load and validate configuration. Fail loudly here rather than three
#      hours into a print.
#   2. Install logging WITH the redaction filter, before anything can log a
#      URL containing the device token.
#   3. Validate the Notify credentials against `/link`. This is the one check
#      that turns "no tiles ever appeared and nothing said why" into a
#      one-line error at boot, and it is also where a GROUP credential is
#      caught, because the Live Activity endpoints would otherwise answer one
#      with a uniform 403 that says nothing useful.
#   4. Recover sessions from the previous run and reconcile them against the
#      gateway, BEFORE any new reading can act on them.
#   5. Start polling BambuBuddy.
#
# Shutdown is equally deliberate: tiles are LEFT ON SCREEN. A restart for a
# routine upgrade should not wipe the Lock Screen of someone watching a print
# that is still running, and step 4 above is what picks them back up.
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any, Optional

from . import BUILD, __version__
from .alerts import Alerts
from .bambuddy_api import APISource, BambuBuddyAuthError, BambuBuddyError
from .config import Config, ConfigError, load_config
from .coordinator import Coordinator
from .health import HealthServer
from .notify_client import NotifyAuthError, NotifyClient, NotifyError
from .redact import RedactingFilter
from .store import Store

log = logging.getLogger("notify_bambuddy")

#: How often the periodic work runs. Fast enough that a pause or a completion
#: reaches the tile within a few seconds, slow enough to be free.
TICK_SECONDS = 5.0

#: Bound on the inbound event queue. A busy five-printer farm produces about
#: five messages a second, so this is minutes of headroom. Bounded rather than
#: unbounded on purpose: if the coordinator ever wedges, dropping progress
#: frames is a far better failure than exhausting memory.
QUEUE_SIZE = 2000


def setup_logging(config: Config) -> None:
    """Configure logging with secret redaction installed at the handler.

    The filter goes on the HANDLER, not on this project's logger, and that is
    the important detail. A traceback raised out of `urllib` is emitted by
    ITS logger, and those are precisely the messages most likely to quote a
    full URL with the device token in its query string. Filtering at the
    handler catches every record regardless of which logger produced it.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.addFilter(RedactingFilter(config.secrets))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.log_level, logging.INFO))

    # urllib is chatty at DEBUG and its messages are rarely what anyone is
    # debugging here.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


async def validate_credentials(client: NotifyClient) -> None:
    """Check the Notify credentials before relying on them.

    `/link` is the only endpoint that reports what a credential POINTS AT.
    Everything on the Live Activity path answers a bad id and a bad token with
    the same uniform 403, by design, so without this check a user with a group
    credential or a typo'd token would get silence and no explanation.
    """
    info = await client.link()
    kind = info.get("type")
    if kind == "group":
        raise ConfigError(
            "Those credentials belong to a Notify GROUP, not a device. A Live Activity is a "
            "tile on one specific iPhone or iPad, so it needs that device's own id and token "
            "from Settings -> Device in the Notify app."
        )
    if kind != "device":
        raise ConfigError(f"Notify did not recognise those credentials (type={kind!r})")
    log.info(
        "Notify credentials are valid: device %s (%s)",
        info.get("id"), info.get("name") or "unnamed",
    )



async def run(config: Config) -> int:
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=QUEUE_SIZE)

    store = Store(config.state_db)
    client = NotifyClient(
        config.notify_base_url, config.device_id, config.device_token, dry_run=config.dry_run
    )

    # --- Credential check --------------------------------------------------
    try:
        await validate_credentials(client)
    except ConfigError as exc:
        log.error("%s", exc)
        store.close()
        return 2
    except NotifyError as exc:
        # Reachability problems are not a reason to refuse to start: the
        # gateway may simply be having a moment, and printer state should keep
        # being tracked meanwhile. A wrong credential, by contrast, is fatal.
        if isinstance(exc, NotifyAuthError):
            log.error(
                "Notify rejected the credentials. Check NOTIFY_DEVICE_ID and "
                "NOTIFY_DEVICE_TOKEN against Settings -> Device in the Notify app."
            )
            store.close()
            return 2
        log.warning("Could not reach Notify to validate credentials (%s); continuing", exc)

    if not config.tiles_enabled:
        # Said once, plainly, because otherwise the first question is "why is
        # there no tile" and the answer is not discoverable from anywhere.
        where = "a browser subscription" if config.device_kind == "browser" else "a Mac"
        log.info(
            "That device is %s, so there will be no Lock Screen tiles: a Live Activity is "
            "an iPhone feature. Every print is still followed, and you still get a "
            "notification when one starts, finishes or fails.", where,
        )

    alerts = Alerts(config, client) if config.alerts != "off" else None
    coordinator = Coordinator(config, store, client, alerts=alerts)

    # --- Recover before consuming -----------------------------------------
    # Deliberately before polling starts. A reading arriving mid-recovery
    # could otherwise create a second tile for a print this service is in the
    # middle of reattaching to.
    try:
        await coordinator.recover()
    except Exception:  # noqa: BLE001 - a bad recovery must not block startup
        log.exception("Session recovery failed; continuing with a clean slate")

    source = APISource(config, loop, queue)
    try:
        await source.validate()
    except BambuBuddyAuthError as exc:
        log.error("%s", exc)
        store.close()
        return 2
    except BambuBuddyError as exc:
        # Unreachable is not fatal, for the same reason a Notify outage is not:
        # BambuBuddy may simply be starting up alongside this, and the poll
        # loop retries with backoff.
        log.warning("Could not reach BambuBuddy at startup (%s); will keep trying", exc)

    health: Optional[HealthServer] = None
    if config.health_enabled:
        def snapshot() -> dict:
            return {
                "version": __version__,
                "source": source.snapshot(),
                "notify": {
                    "device_id": config.device_id,
                    "dry_run": config.dry_run,
                    "alerts": config.alerts,
                },
                **coordinator.snapshot(),
            }

        try:
            health = HealthServer(config.health_host, config.health_port, snapshot)
            health.start()
        except OSError as exc:
            log.warning("Could not start the health endpoint on %s:%d (%s); continuing without it",
                        config.health_host, config.health_port, exc)
            health = None

    source.start()

    stopping = asyncio.Event()

    def request_stop() -> None:
        log.info("Shutting down; live tiles are left on screen and picked up on the next start")
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows, or a non-main thread. The KeyboardInterrupt path below
            # still works.
            pass

    log.info("notify-bambuddy %s (build %s) is running", __version__, BUILD)

    prune_counter = 0
    try:
        while not stopping.is_set():
            # Drain whatever has arrived, then tick. Waiting with a timeout
            # rather than polling means a print starting is picked up
            # immediately, while the periodic work still runs on schedule when
            # nothing is happening at all.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                event = None

            if event is not None:
                try:
                    await coordinator.handle(event)
                except Exception:  # noqa: BLE001 - one bad event must not stop the service
                    log.exception("Failed to handle %s", type(event).__name__)

            try:
                await coordinator.tick()
            except Exception:  # noqa: BLE001
                log.exception("Periodic work failed")

            prune_counter += 1
            # Roughly every ten minutes at the default tick. Inside its own
            # guard: a housekeeping DELETE failing (disk full, a locked file)
            # is not a reason to take the whole service down.
            if prune_counter >= 120:
                prune_counter = 0
                try:
                    store.prune()
                except Exception:  # noqa: BLE001
                    log.exception("Store housekeeping failed; will try again later")
    except KeyboardInterrupt:
        request_stop()
    finally:
        source.stop()
        if health is not None:
            health.stop()
        store.close()

    return 0


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # Logging is not configured yet, and a configuration error is exactly
        # the message a user needs to see even if their log pipeline is not
        # working, so it goes straight to stderr.
        print(f"notify-bambuddy: {exc}", file=sys.stderr)
        return 2

    setup_logging(config)

    if config.dry_run:
        log.warning("DRY_RUN is on: no Live Activities or notifications will actually be sent")

    if config.printer_allowlist and len(config.printer_allowlist) > config.max_tiles:
        # Not fatal, but worth one line: a sixth printer waits for a slot
        # rather than failing, which otherwise looks like a bug.
        log.warning(
            "PRINTER_ALLOWLIST names %d printers but MAX_TILES is %d; the extra printers "
            "will queue for a tile slot rather than all showing at once",
            len(config.printer_allowlist), config.max_tiles,
        )

    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
