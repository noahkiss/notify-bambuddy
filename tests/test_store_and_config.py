# ---------------------------------------------------------------------------
# Persistence, configuration validation, and secret redaction.
#
# The redaction tests are the ones that matter most here. The Notify API
# carries its credential in a QUERY PARAMETER, which is what makes it usable
# as a webhook URL and also means every URL this service builds contains a
# live token. A user pasting logs into a GitHub issue must not be publishing
# push access to their phone.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import time

import pytest
from conftest import SERIAL

from notify_bambuddy.config import ConfigError, load_config
from notify_bambuddy.models import Delivery, Outcome, Phase, SessionState
from notify_bambuddy.redact import PLACEHOLDER, RedactingFilter, redact_text, redact_url
from notify_bambuddy.store import Store

TOKEN = "XYZ789TOKEN123"
API_KEY = "bb_SECRETKEY12345"

MINIMAL_ENV = {
    "BAMBUDDY_URL": "http://bambuddy.local:8000",
    "BAMBUDDY_API_KEY": API_KEY,
    "NOTIFY_DEVICE_ID": "ABC12345",
    "NOTIFY_DEVICE_TOKEN": TOKEN,
}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_a_token_in_a_query_string_is_redacted():
    url = f"https://push.getnotifyapp.com/live-activity/ABC12345?token={TOKEN}&progress=94"
    assert TOKEN not in redact_text(url)
    assert "progress=94" in redact_text(url)


def test_a_bare_token_is_redacted_when_the_value_is_known():
    """Catches the case where a credential appears without its `token=` prefix,
    for example inside a JSON body or a hand-written log line."""
    assert TOKEN not in redact_text(f"using {TOKEN} for auth", [TOKEN])


def test_a_password_in_a_url_is_redacted_but_the_username_survives():
    """Keeping the username makes the line still useful for debugging a wrong
    user, which is the mistake people actually make."""
    result = redact_text("connecting to https://bambuddy:hunter2@host.local:8000")
    assert "hunter2" not in result
    assert "bambuddy" in result


def test_redacting_a_url_drops_the_whole_query_string():
    """Stronger than field-level redaction on purpose: the Live Activity API
    can carry the entire tile payload in the query string, including a print
    filename the user may consider private."""
    url = f"https://host/live-activity/LA7Q2ZKM?token={TOKEN}&title=Secret%20Project"
    assert redact_url(url) == "https://host/live-activity/LA7Q2ZKM?<redacted>"


def test_a_very_short_secret_is_not_used_for_literal_matching():
    """A three-character "secret" would match everywhere and turn the logs
    into noise. Real Notify tokens are fifteen characters."""
    assert redact_text("the cat sat on the mat", ["cat"]) == "the cat sat on the mat"


def test_the_logging_filter_scrubs_records_from_any_logger(caplog):
    """The filter is installed on the HANDLER, not on this project's logger,
    because a traceback quoting a full URL is emitted by urllib's logger, and
    those are the records most likely to carry a token."""
    logger = logging.getLogger("some.third.party.library")
    log_filter = RedactingFilter([TOKEN])

    record = logger.makeRecord(
        "some.third.party.library", logging.ERROR, __file__, 1,
        "request failed: %s",
        (f"https://host/live-activity/ABC12345?token={TOKEN}",), None,
    )
    assert log_filter.filter(record)
    assert TOKEN not in record.getMessage()
    assert PLACEHOLDER in record.getMessage()


def test_the_filter_scrubs_dictionary_arguments_too():
    log_filter = RedactingFilter([TOKEN])
    # Wrapped in a tuple, which is how `logger.info("%(url)s", {...})` reaches
    # LogRecord before it unwraps a single Mapping argument.
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "%(url)s", ({"url": f"?token={TOKEN}"},), None)
    log_filter.filter(record)
    assert TOKEN not in record.getMessage()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_a_group_id_is_rejected_before_it_can_fail_silently():
    """The Live Activity endpoints answer a group credential with the same
    uniform 403 they use for a bad token, which tells the user nothing."""
    with pytest.raises(ConfigError) as caught:
        load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="GRP12345"))
    assert "group" in str(caught.value).lower()


def test_a_web_push_device_id_turns_off_tiles_instead_of_being_rejected():
    """A browser cannot host a Live Activity, but it can be notified.

    Refusing to start would withhold "your print finished" from someone whose
    only sin is reading their notifications in a browser, which is most of the
    value of this service.
    """
    config = load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="WB12345678901234"))
    assert config.tiles_enabled is False


def test_an_ios_device_id_keeps_tiles_on():
    assert load_config(MINIMAL_ENV).tiles_enabled is True


def test_a_browser_with_alerts_off_is_rejected():
    """That combination leaves nothing this service is allowed to send, and
    failing at startup is far kinder than running silently forever."""
    with pytest.raises(ConfigError) as caught:
        load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="WB12345678901234", ALERTS="off"))
    assert "ALERTS" in str(caught.value)


@pytest.mark.parametrize("bad", [
    "WB123", "WB123456789012345", "ABCD123", "abcd1234",
    # The 16-character namespaces are uppercase on the gateway, so a lowercase
    # one cannot be a real id. Better refused here, with an explanation, than
    # accepted and then refused by the gateway with a uniform 404.
    "IOabcdefgh123456", "WBabcdefgh123456", "MCabcdefgh123456",
    # 16 characters with no recognised prefix: shape alone must not be enough.
    "ABCDEFGH12345678", "ZZABCDEFGH123456",
    "IO1234567890123", "IO12345678901234X",
])
def test_a_device_id_of_the_wrong_shape_is_still_rejected(bad):
    with pytest.raises(ConfigError):
        load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID=bad))


# --- Issue #1: Notify's id namespaces --------------------------------------
#
# The gateway mints five shapes. Every iPhone installed from app 6.09.02 on
# gets an `IO` id, and until 0.5.1 this service accepted only the 8-character
# shape for a phone, so every one of those was refused at startup with valid
# credentials.


def test_a_new_iphone_id_starting_io_gets_tiles():
    """The reported bug. `IO` plus 14 is what a fresh install mints."""
    config = load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="IOYHABCDEFGH1234"))
    assert config.tiles_enabled is True
    assert config.device_kind == "iphone"


def test_a_legacy_eight_character_id_still_gets_tiles():
    """Phones that already hold an 8-character id keep it forever."""
    config = load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="ABCD1234"))
    assert config.tiles_enabled is True


@pytest.mark.parametrize("legacy", ["IO123456", "WB123456", "MC123456"])
def test_an_eight_character_id_is_legacy_whatever_its_first_two_letters(legacy):
    """Namespaces are told apart by prefix AND length. A legacy id that
    happens to start with a reserved prefix is still a legacy id, which is how
    the gateway's own register branches read it."""
    assert load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID=legacy)).tiles_enabled is True


def test_a_mac_id_runs_notifications_only():
    """A Live Activity is an iPhone feature. The fix proposed in the issue
    widened the phone pattern to any 16 uppercase characters, which would have
    accepted a Mac id as able to host a tile."""
    config = load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="MCABCDEFGH123456"))
    assert config.tiles_enabled is False
    assert config.device_kind == "mac"


def test_a_browser_id_is_still_a_browser_now_that_phones_have_sixteen_characters():
    """All three 16-character namespaces are the same length, so only the
    prefix can separate them."""
    config = load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="WBABCDEFGH123456"))
    assert config.tiles_enabled is False
    assert config.device_kind == "browser"


def test_a_mac_with_alerts_off_is_rejected_and_says_mac():
    with pytest.raises(ConfigError) as caught:
        load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="MCABCDEFGH123456", ALERTS="off"))
    assert "a Mac" in str(caught.value)


def test_the_rejection_message_names_the_new_shape():
    """Someone whose phone shows an IO id must be able to tell from the error
    that their id format is a real one."""
    with pytest.raises(ConfigError) as caught:
        load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="nonsense"))
    assert "'IO'" in str(caught.value)
def test_a_tint_accepts_both_hex_forms_and_normalises_them():
    assert load_config(dict(MINIMAL_ENV, NOTIFY_TINT="7C3AED")).tint == "#7C3AED"
    assert load_config(dict(MINIMAL_ENV, NOTIFY_TINT="#7C3AED")).tint == "#7C3AED"
    with pytest.raises(ConfigError):
        load_config(dict(MINIMAL_ENV, NOTIFY_TINT="rebeccapurple"))


def test_a_secret_can_be_supplied_from_a_file(tmp_path):
    """How Docker and Kubernetes secrets arrive: as a mounted file, invisible
    to `docker inspect` and to the process environment."""
    secret_file = tmp_path / "token"
    secret_file.write_text(TOKEN + "\n")
    config = load_config({
        "BAMBUDDY_URL": "http://bambuddy.local:8000",
        "BAMBUDDY_API_KEY": API_KEY,
        "NOTIFY_DEVICE_ID": "ABC12345",
        "NOTIFY_DEVICE_TOKEN_FILE": str(secret_file),
    })
    assert config.device_token == TOKEN


def test_secrets_are_collected_for_the_log_filter():
    config = load_config(MINIMAL_ENV)
    assert TOKEN in config.secrets
    assert API_KEY in config.secrets, "the API key must be redacted from logs too"


def test_an_unknown_alert_level_is_rejected():
    with pytest.raises(ConfigError):
        load_config(dict(MINIMAL_ENV, ALERTS="sometimes"))


def test_the_allowlist_scopes_which_printers_get_tiles():
    config = load_config(dict(MINIMAL_ENV, PRINTER_ALLOWLIST=f" {SERIAL} , OTHER, "))
    assert config.printer_allowlist == (SERIAL, "OTHER")
    assert config.allows(SERIAL)
    assert not config.allows("SOMETHING_ELSE")
    # An empty allowlist means everything is in scope.
    assert load_config(dict(MINIMAL_ENV)).allows("anything")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def make_session(**overrides) -> SessionState:
    values = dict(
        session_key=f"{SERIAL}:1000",
        serial=SERIAL,
        printer_name="X1C-1",
        phase=Phase.PRINTING,
        delivery=Delivery.ACTIVE,
        activity_id="LA000001",
        job_name="Benchy",
        progress=45.5,
        eta_seconds=6000,
        layer_num=150,
        total_layers=300,
        created_at=1000.0,
    )
    values.update(overrides)
    return SessionState(**values)


def test_a_session_round_trips_through_the_database():
    store = Store(":memory:")
    store.save_session(make_session(outcome=Outcome.COMPLETED))
    loaded = store.get_session(f"{SERIAL}:1000")
    assert loaded.serial == SERIAL
    assert loaded.phase is Phase.PRINTING
    assert loaded.outcome is Outcome.COMPLETED
    assert loaded.eta_seconds == 6000


def test_saving_the_same_session_twice_updates_rather_than_duplicates():
    store = Store(":memory:")
    store.save_session(make_session())
    store.save_session(make_session(progress=88.0))
    assert len(store.load_live_sessions()) == 1
    assert store.get_session(f"{SERIAL}:1000").progress == 88.0


def test_only_in_flight_sessions_are_recovered():
    store = Store(":memory:")
    store.save_session(make_session(session_key="a", delivery=Delivery.ACTIVE))
    store.save_session(make_session(session_key="b", delivery=Delivery.ENDED))
    store.save_session(make_session(session_key="c", delivery=Delivery.DISMISSED))
    store.save_session(make_session(session_key="d", delivery=Delivery.ENDING))
    assert {s.session_key for s in store.load_live_sessions()} == {"a", "d"}


def test_a_dismissal_survives_a_restart_and_is_cleared_by_a_new_print(tmp_path):
    """The reason dismissals are keyed by printer rather than by session: a
    session key is derived from the moment a print was detected and cannot be
    reconstructed after a restart."""
    path = str(tmp_path / "state.db")
    store = Store(path)
    store.record_dismissal(SERIAL)
    store.close()

    reopened = Store(path)
    assert reopened.is_dismissed(SERIAL)
    reopened.clear_dismissal(SERIAL)
    assert not reopened.is_dismissed(SERIAL)
def test_pruning_drops_expired_rows_but_keeps_live_ones():
    store = Store(":memory:")
    store.save_session(make_session(delivery=Delivery.ACTIVE))
    store._db.execute("INSERT INTO dismissals (serial, dismissed_at) VALUES (?,?)",
                      ("STALE", time.time() - 999999))
    store._db.commit()

    store.prune()

    assert not store.is_dismissed("STALE")
    assert len(store.load_live_sessions()) == 1


def test_meta_values_round_trip():
    store = Store(":memory:")
    assert store.get_meta("missing") is None
    store.set_meta("schema", "1")
    assert store.get_meta("schema") == "1"


# ---------------------------------------------------------------------------
# The gateway host
# ---------------------------------------------------------------------------


def test_the_gateway_host_is_the_current_one():
    """Pinned deliberately. The host moved once (from `notifypush.pingie.com`
    to `push.getnotifyapp.com`), and a stale constant would send every tile to
    a name that may stop resolving without anything here noticing."""
    from notify_bambuddy.config import NOTIFY_BASE_URL

    assert NOTIFY_BASE_URL == "https://push.getnotifyapp.com"
    assert NOTIFY_BASE_URL.startswith("https://"), "never plain HTTP: the token is in the URL"


def test_nothing_trusts_the_host_the_link_response_quotes():
    """`/link` echoes a `notification_url` built from the gateway's own older
    hostname. Reading it would silently move this service back to whichever
    name the server happens to mention; the base URL is our constant."""
    import inspect

    from notify_bambuddy import __main__ as entry
    from notify_bambuddy import notify_client

    for module in (entry, notify_client):
        assert "notification_url" not in inspect.getsource(module), \
            f"{module.__name__} must not read the URL out of a gateway response"
def test_abandon_after_must_outlast_silence_after():
    """Otherwise the tile is ended before it can ever say "Connection lost"."""
    with pytest.raises(ConfigError):
        load_config(dict(MINIMAL_ENV, SILENCE_AFTER="600", ABANDON_AFTER="300"))


def test_an_invalid_log_level_is_rejected_rather_than_silently_ignored():
    with pytest.raises(ConfigError):
        load_config(dict(MINIMAL_ENV, LOG_LEVEL="CHATTY"))


def test_the_default_state_path_is_writable_by_an_ordinary_user():
    """The documented `pip install .` path must not die on its first run.

    The container sets STATE_DB explicitly and mounts a volume; a user running
    it natively is an ordinary account that cannot create /data.
    """
    config = load_config(dict(MINIMAL_ENV))
    assert not config.state_db.startswith("/data/")
    store = Store(config.state_db)
    store.close()
    import os
    os.remove(config.state_db)


# ---------------------------------------------------------------------------
# Fork: a self-hosted Notify-compatible server
# ---------------------------------------------------------------------------


def test_the_base_url_defaults_to_the_gateway():
    from notify_bambuddy.config import NOTIFY_BASE_URL

    assert load_config(MINIMAL_ENV).notify_base_url == NOTIFY_BASE_URL


def test_the_base_url_can_be_overridden():
    config = load_config(dict(MINIMAL_ENV, NOTIFY_BASE_URL="http://push.example:4372/"))
    assert config.notify_base_url == "http://push.example:4372"


@pytest.mark.parametrize("bad", ["ftp://push.example", "push.example", "http://", "https://push.example/?a=1"])
def test_a_bad_base_url_is_refused(bad):
    with pytest.raises(ConfigError, match="NOTIFY_BASE_URL"):
        load_config(dict(MINIMAL_ENV, NOTIFY_BASE_URL=bad))


def test_a_self_hosted_server_takes_any_device_id():
    config = load_config(dict(
        MINIMAL_ENV, NOTIFY_BASE_URL="http://push.example:4372", NOTIFY_DEVICE_ID="x2d"))
    assert config.tiles_enabled is True
    with pytest.raises(ConfigError, match="not a Notify device id"):
        load_config(dict(MINIMAL_ENV, NOTIFY_DEVICE_ID="x2d"))
