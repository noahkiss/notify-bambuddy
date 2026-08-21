# ---------------------------------------------------------------------------
# notify-bambuddy
#
# A sidecar service that turns BambuBuddy print jobs into Notify! Live
# Activities: one Lock Screen tile per printer that appears when a print
# starts, updates in place while it runs, and ends when it finishes.
#
# It talks to two systems and owns neither:
#
#   notify-bambuddy  --(HTTPS GET, read only)-->  BambuBuddy
#   notify-bambuddy  --(HTTPS)-->  push.getnotifyapp.com  --(APNs)-->  iPhone
#
# It reads BambuBuddy by polling two endpoints with an API key:
#
#   GET /api/v1/webhook/queue                 which printers exist
#   GET /api/v1/webhook/printer/{id}/status   state, progress, ETA, job name
#
# Design constraints that shaped everything else:
#
#   * READ ONLY toward BambuBuddy. Two GET requests, and the key they use
#     needs only `read_status`. It never sends a printer a command and never
#     calls an endpoint that changes anything. If it crashes, printing is
#     completely unaffected.
#
#   * NO PUBLIC LISTENER. Both connections are outbound, so BambuBuddy never
#     has to reach back into this. There is nothing to reverse-proxy and
#     nothing to port-forward. The only bound socket is a health endpoint,
#     and it binds to localhost by default.
#
#   * THE NUMBERS LIE, SOMETIMES. BambuBuddy does not reset progress at print
#     start, its state field is an unvalidated passthrough of the firmware's
#     own string, and Notify's server can reap a tile out from under us. Every
#     module here assumes its inputs are late, duplicated, out of order, or
#     simply wrong, because against real printers all four happen.
#
# Licensed AGPL-3.0-or-later. See LICENSE.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os

__version__ = "0.2.1"

#: The commit this build came from, stamped into the image at build time.
#: "unknown" when running from a checkout rather than a published image.
#:
#: This exists because a rolling `latest` tag makes "which version am I
#: running" unanswerable from the version number alone: two images months
#: apart can both say 0.2.0. The commit cannot be ambiguous.
BUILD = os.environ.get("NOTIFY_BAMBUDDY_BUILD", "unknown")

# The user agent sent on every Notify API call. Having the version in here
# means a gateway operator looking at logs can tell which release is
# misbehaving without asking the user.
USER_AGENT = f"notify-bambuddy/{__version__}"

__all__ = ["__version__", "USER_AGENT"]
