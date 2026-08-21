# ---------------------------------------------------------------------------
# Secret redaction.
#
# This module exists because of one specific failure mode: a user hits a
# problem, pastes their logs into a GitHub issue or a Discord channel, and
# their Notify device token goes public. That token is a bearer credential.
# Anyone holding it can push notifications and Live Activity tiles to the
# user's phone until they delete and re-register the device.
#
# The Notify API deliberately passes the token as a QUERY PARAMETER
# (`?token=...`), because that is what makes it usable as a webhook URL. That
# design decision is great for users pasting a URL into a settings box and
# terrible for logging: every URL this service touches has a live credential
# baked into it. So the rule here is absolute:
#
#     NO CODE IN THIS PROJECT MAY LOG A FULL NOTIFY URL.
#
# Two layers enforce it:
#
#   1. `redact_text()` scrubs known secret shapes out of any string.
#   2. `RedactingFilter` is installed on the root logger, so even a message
#      that forgot to call `redact_text()` gets scrubbed on the way out. Belt
#      and braces, because the cost of missing one is a leaked credential.
#
# Layer 2 is the important one. Layer 1 alone relies on every future
# contributor remembering; the filter does not.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import re
from typing import Iterable

# The placeholder that replaces every redacted value. Deliberately loud, so
# that a redacted log line is obviously redacted rather than looking like a
# truncated value.
PLACEHOLDER = "***REDACTED***"

# ---------------------------------------------------------------------------
# Pattern-based redaction
#
# These patterns catch secrets by SHAPE, which covers the case where a secret
# reaches the logs through a path this module never registered, for example an
# exception message raised from inside urllib that quotes the request URL.
# ---------------------------------------------------------------------------

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # `token=...` / `?token=abc123` anywhere in a URL or query string. This is
    # the Notify device token, and it is the single most important thing on
    # this list. Stops at the next `&`, whitespace, quote, or end of string.
    re.compile(r"(?i)(\btoken=)([^&\s\"'<>]+)"),
    # `password=...` anywhere, such as a config dump or a third-party URL.
    re.compile(r"(?i)(\bpassword=)([^&\s\"'<>]+)"),
    # Credentials embedded in a URL's authority section, as in
    # `https://user:hunter2@host.local:8000`. Only the password half is
    # replaced; keeping the username makes the log line still useful for
    # debugging "wrong user" mistakes.
    re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)"),
    # An `Authorization: Bearer ...` header, in case one is ever logged.
    re.compile(r"(?i)(\bauthorization:\s*bearer\s+)(\S+)"),
)


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return `text` with every recognised secret replaced by PLACEHOLDER.

    Two mechanisms run, in this order:

    1. Literal replacement of `extra_secrets`. These are the exact secret
       VALUES the process knows about: the Notify device token and the
       BambuBuddy API key. Literal matching catches a secret that appears
       without its
       `token=` prefix, for example in a JSON body or a bare log message.

    2. Pattern replacement. These catch secrets whose value this process does
       not know, which matters for values that arrive from elsewhere, and they
       catch the configured secrets too if step 1 somehow missed the encoding.

    Literal-first matters: it means a token that appears as `token=abc` is
    already gone before the pattern pass runs, and a token that appears bare is
    still caught. Neither pass can reintroduce a secret.
    """
    if not text:
        return text

    result = text

    # Step 1: exact values. Sorted longest-first so that a secret which is a
    # substring of another secret cannot leave a partial value behind.
    for secret in sorted((s for s in extra_secrets if s), key=len, reverse=True):
        # A very short "secret" would match everywhere and turn the logs into
        # noise. Anything under 6 characters is almost certainly not a real
        # credential, and Notify tokens are 15 characters.
        if len(secret) >= 6:
            result = result.replace(secret, PLACEHOLDER)

    # Step 2: shape-based patterns. `\g<1>` keeps the key name so the reader
    # can still tell WHICH secret was redacted.
    for pattern in _PATTERNS:
        if pattern.groups == 3:
            # The URL-authority pattern, which has a trailing `@` to preserve.
            result = pattern.sub(r"\g<1>" + PLACEHOLDER + r"\g<3>", result)
        else:
            result = pattern.sub(r"\g<1>" + PLACEHOLDER, result)

    return result


def redact_url(url: str) -> str:
    """Redact a URL for logging.

    Slightly stronger than `redact_text` on a URL: the entire query string is
    dropped rather than selectively scrubbed. Notify's Live Activity API can
    carry the whole tile payload in the query string (the "one static URL is
    the whole API" dialect), and that payload can include a print filename,
    which the user may consider private. The path is what a reader actually
    needs in order to debug, so the path is what survives.

    `https://host/live-activity/LA7Q2ZKM?token=secret&title=Benchy`
        becomes
    `https://host/live-activity/LA7Q2ZKM?<redacted>`
    """
    if not url:
        return url
    head, sep, _query = url.partition("?")
    return head + ("?<redacted>" if sep else "")


class RedactingFilter(logging.Filter):
    """A logging filter that scrubs secrets from every record that passes it.

    Installed on the ROOT logger handler rather than on this project's own
    logger, deliberately. A stack trace raised out of `urllib` is emitted by
    ITS logger, and those are exactly the messages most likely to quote a full
    URL. Filtering at the handler catches every one of them.

    The filter mutates `record.msg` and `record.args` rather than only the
    formatted output, so the redaction survives any downstream formatter.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # Stored as a tuple so the set of secrets cannot be mutated by accident
        # after installation.
        self._secrets: tuple[str, ...] = tuple(s for s in secrets if s)

    #: Used to render a traceback ahead of the real formatter. A bare
    #: Formatter's `formatException` produces the same text the handler's
    #: formatter would, so pre-rendering here changes nothing but timing.
    _renderer = logging.Formatter()

    def _scrub(self, value):
        """Redact one lazy-format argument, whatever its type.

        Strings are redacted directly. Anything else (an exception, a URLError
        whose `reason` quotes the URL, an object with a chatty `__repr__`) is
        rendered to text and redacted only if that changed something, so an
        `int` destined for a `%d` is left as an int.
        """
        if isinstance(value, str):
            return redact_text(value, self._secrets)
        try:
            rendered = str(value)
        except Exception:  # noqa: BLE001 - never let a __str__ bug break logging
            return value
        cleaned = redact_text(rendered, self._secrets)
        return cleaned if cleaned != rendered else value

    def filter(self, record: logging.LogRecord) -> bool:
        # The message template itself.
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg, self._secrets)

        # Lazy-formatting arguments, which is where a URL passed as
        # `log.info("calling %s", url)` actually lives.
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: self._scrub(value) for key, value in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(value) for value in record.args)

        # The traceback. This is the subtle one: `Formatter.format` renders
        # `exc_info` into `record.exc_text` only AFTER filters have run, and
        # then reuses `exc_text` if it is already set. So the traceback is
        # rendered here first, redacted, and stashed, and the formatter picks
        # up the redacted version. Without this, `log.exception(...)` would
        # print a traceback whose exception message can carry a full URL.
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = self._renderer.formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, self._secrets)

        # Always returns True: this filter redacts, it never drops records.
        return True
