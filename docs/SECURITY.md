# Security

## What this service can reach

Two outbound connections, and nothing else:

| To | Protocol | Direction | Why |
|---|---|---|---|
| Your BambuBuddy (`BAMBUDDY_URL`) | HTTP(S) | Outbound, `GET` only | Read printer state |
| `push.getnotifyapp.com` | HTTPS | Outbound | Create and update tiles |

It **never**:

- connects to a printer
- calls a BambuBuddy control endpoint. It uses exactly two routes,
  `GET /api/v1/webhook/queue` for discovery and
  `GET /api/v1/webhook/printer/{id}/status` for telemetry. Both are `GET`,
  both need only the `read_status` permission, and no call to any other
  BambuBuddy route exists anywhere in the code.
- accepts an inbound connection from anywhere except the health endpoint

There is no webhook receiver, so there is nothing to reverse-proxy, nothing to
port-forward, and no unauthenticated endpoint that could be reached from the
internet by accident. The only listening socket is the health endpoint, bound
to localhost by default. This service polls BambuBuddy, and BambuBuddy never
calls back.

The Notify gateway URL is a compile-time constant, not configuration, so no
input can redirect the push traffic somewhere else. `BAMBUDDY_URL` is
configuration, because it names a host you run, and it is only ever used to
build the two `GET` requests above.

---

## The credentials that matter

Two of them, and they are worth protecting for different reasons.

### `NOTIFY_DEVICE_TOKEN`

`NOTIFY_DEVICE_TOKEN` is a bearer credential. Anyone holding it can push
notifications and Lock Screen tiles to your phone. It does not grant access to
anything else, but that is enough to be worth protecting.

The Notify API carries it as a **query parameter**, which is what makes the
same address usable as a webhook URL, and which means every URL this service
builds contains a live credential. Two layers keep it out of the logs:

1. Known secret values are replaced literally. Both the device token and the
   BambuBuddy API key are in that set.
2. A pattern pass catches `token=`, `password=`, credentials in a URL
   authority, and `Authorization: Bearer` headers by shape, which covers
   secrets this process does not know about.

The redaction filter is installed on the **root logging handler**, not on this
project's own logger. That is deliberate: a traceback raised from inside
`urllib` is emitted by *its* logger, and records like that are exactly the ones
most likely to quote a full URL. Filtering at the handler catches every record
regardless of origin, so no future contributor has to remember.

Any Notify URL that does get logged has its **entire query string** dropped,
not just its token, because the URL dialect can carry the whole tile payload
including a print filename.

`tests/test_store_and_config.py` pins all of this.

### `BAMBUDDY_API_KEY`

It grants read access to your BambuBuddy, which is a smaller blast radius than
the device token but still worth keeping quiet.

It needs the `read_status` permission and nothing else. Issue it with that
permission alone. Nothing in this service calls a control route, so a wider
key buys you no functionality and only widens what a leak would cost: a key
that cannot start, pause or cancel a print cannot be used to start, pause or
cancel one.

It travels in the `X-API-Key` **header**. It is never placed in a URL, a query
string, or a path, so the whole class of leaks that makes the device token
awkward (proxy access logs, referrers, a URL quoted whole in a traceback) does
not apply to it. It is redacted from logs anyway, by the same literal
replacement pass, on the same root handler.

The header is only as private as the transport under it. If `BAMBUDDY_URL` is
`http://`, the `X-API-Key` header crosses your local network in clear text like
everything else in that request, readable by anything on the path. On a wire
you do not fully trust, point it at `https://`.

HTTPS to the Notify gateway always verifies, with no override.

### Supplying them safely

Best to worst:

```bash
# Container or orchestrator secret, mounted as a file. Not in the process
# environment, not in `docker inspect`.
NOTIFY_DEVICE_TOKEN_FILE=/run/secrets/notify_token
BAMBUDDY_API_KEY_FILE=/run/secrets/bambuddy_api_key
```

```bash
# An env file next to your Compose file, readable only by you.
chmod 600 notify-bambuddy.env
```

Avoid passing either on a command line: the whole command is visible in `ps`
to every user on the machine.

`.env` is gitignored. If you ever commit a token, treat it as burned: delete
the device in the Notify app and re-register. A committed API key is burned
the same way: revoke it in BambuBuddy and issue a new one, again with
`read_status` only. Rotating either is cheap.

---

## Running it

The container image:

- runs as uid 10001, never root
- has no compiler, no build cache, and no shell utilities beyond the base
- runs read-only in the supplied Compose file, with `cap_drop: ALL` and
  `no-new-privileges`
- writes to exactly one directory, `/data`

It needs privileged access to nothing, and nothing is installed on the host:
the container is the whole footprint, and removing it removes everything.

---

## What is stored

The SQLite database holds:

- printer identifiers, printer names and job names. The API exposes no serial,
  so the identifier is `"bb"` plus BambuBuddy's own database id, for example
  `bb1`. It survives a rename.
- Live Activity ids and timestamps
- which tiles were dismissed
- message fingerprints for duplicate suppression

**No credential is ever written to it**, neither the device token nor the
BambuBuddy API key. Both live only in the process environment, or in the file
a `_FILE` variable points at, and in memory.

`PRIVACY_MODE=true` keeps model and file names off the Lock Screen and out of
the alerts. It does not remove them from the local database, where they are
needed to build a tile.

---

## The health endpoint

Binds to `127.0.0.1` by default. In the container image it binds to `0.0.0.0`,
because that is what makes Docker and Kubernetes health checks work and the
container network is the boundary there.

It exposes printer names, job names and printer identifiers. It exposes no
credential: not the device token, not the API key, and no Notify URL.

The snapshot does carry one URL, the `BAMBUDDY_URL` you configured, under the
`source` key. That is the address of a host you run, not a credential. Keep it
that way: the API key belongs in the `X-API-Key` header, which is where this
service puts it, and nothing needs it in the URL.

It is safe to paste into an issue report; it is not something to publish on a
network where those names or that address are sensitive.

---

## Untrusted input

Everything BambuBuddy hands over is treated as untrusted:

- Non-JSON and non-object payloads are logged and dropped.
- Every field is coerced with a safe default rather than trusted.
- Strings that reach a tile are trimmed to the API's limits and have their
  whitespace collapsed.
- Numbers are clamped to their valid ranges.
- The event queue is bounded, so a burst drops events rather than exhausting
  memory.

Identity comes from outside the payload. It is the printer id in the URL that
was requested, so a `status` body cannot claim to be a printer other than the
one that was asked for.

One malformed response cannot stop the poller: parsing failures are caught per
response, and one failed poll does not end the loop. HTTP 401 and 403 are the
exception: they are fatal at startup, exit code 2, carrying BambuBuddy's own
`detail` text, because a rejected key does not become accepted by being
retried. Everything else backs off and tries again.

---

## Reporting a vulnerability

Open a
[security advisory](https://github.com/simplytoast1/notify-bambuddy/security/advisories/new)
rather than a public issue.

Particularly interested in: any path that leaks the device token or the
BambuBuddy API key into logs, diagnostics, or the health endpoint; any way to
make it issue a BambuBuddy request that is not one of the two `GET`s above, or
that needs a permission beyond `read_status`; any way to make it call a host
other than the hardcoded gateway and your configured `BAMBUDDY_URL`.
