# Calculator, operated

A stdlib-only HTTP/1.1 calculator (the original project is described
[below](#a-calculator-that-stays-on-the-line)), now built, tested, scanned,
deployed to Kubernetes, and observed.

- **Baseline:** [`35c6f47`](https://github.com/lokendra005/calculator_Assignment/tree/35c6f47522a3be930b3fb8c4389302b4bb68b9d8)
  (tag `baseline`). That is the app as it was: `server.py`, `test_client.py` and this
  README's second half. There was no container, CI, Kubernetes config, metrics, or health endpoints.
- **Design decisions and trade-offs:** [docs/DECISIONS.md](docs/DECISIONS.md)
- **Alerts → actions, failure drills:** [docs/RUNBOOK.md](docs/RUNBOOK.md)
- **Evidence from real runs:** [docs/EVIDENCE.md](docs/EVIDENCE.md)

## What was added

| Area | Where |
|---|---|
| `/healthz` `/readyz` `/version` `/metrics`, JSON logs, W3C trace ids, graceful SIGTERM drain | `server.py` |
| Distroless non-root image, version baked in | `Dockerfile` |
| Helm chart: Deployment (2 replicas, probes, resources, hardened securityContext), NodePort Service, PDB, NetworkPolicy, ServiceMonitor, SLO alert rules, Grafana dashboard | `chart/calculator/` |
| CI: lint, protocol tests, helm lint, kubeconform, Trivy config + image scans, deploy to kind, smoke test, then publish to GHCR | `.github/workflows/ci.yml` |
| Local cluster, monitoring stack, deploy / rollback / drift / status targets | `Makefile`, `deploy/` |

## Run it

**Prerequisites:** Docker (2 GB of memory is enough), `kind` ≥ 0.24, `kubectl`, `helm` 4,
`make`, `curl`, Python 3. On macOS: `brew install kind helm kubectl`.

```bash
make cluster          # kind cluster "calc"; NodePort 30080 → localhost:30080
make monitoring       # Prometheus + Grafana + kube-state-metrics (~3 min)
make deploy           # deploys the CI-built image for HEAD: ghcr.io/lokendra005/calculator:<sha>
                      #   or: make deploy SHA=<any commit CI has built>
                      #   offline/dev: make build-local (builds HEAD on this machine)
```

`make deploy` ends with `deploy/smoke.sh`, which checks real arithmetic, an error
case, and that `/version` equals the sha you asked for.

**Access**

| What | How |
|---|---|
| App | `curl "localhost:30080/add?a=2&b=3"`, `curl localhost:30080/version` |
| Prometheus | `make prometheus` → http://localhost:9090 (alerts: `/alerts`) |
| Grafana | `make grafana` prints the generated admin password → http://localhost:3000, user `admin`, dashboard "Calculator" |
| Logs | `kubectl -n calculator logs -l app.kubernetes.io/name=calculator -f` |

**Operate**

```bash
make status           # serving sha, image, helm history
make drift            # live objects vs chart for the deployed sha (exit 1 = drift)
make rollback         # previous revision, then smoke test
make test             # protocol + ops tests against a local process (no cluster)
python3 deploy/load.py 60   # steady traffic, reports every failed request
make down             # delete the cluster
```

**Known limitations**

- Single-node kind: a node failure is a full outage. The topology spread
  constraint only matters on more nodes.
- Deploying to the laptop cluster is a manual `make deploy`, because GitHub
  cannot reach it. CI proves each image on an ephemeral kind cluster first.
  See DECISIONS.md, "CI/CD and the automation boundary".
- No Alertmanager: alerts evaluate and show as firing in Prometheus but are
  not routed anywhere. No log aggregation (no Loki): logs are read with `kubectl logs`.
- Tracing is trace-id propagation and log correlation, with no span export.
  There is a single hop.
- No TLS: plain HTTP on localhost.

---

# A calculator that stays on the line

An HTTP/1.1 calculator written directly against a TCP socket. No framework, no
`http.server`, no third-party packages — just `socket`, and the parsing you have
to do yourself once you decide not to hang up after every reply.

```
GET /add?a=2&b=3   -> 200  5
GET /sub?a=10&b=4  -> 200  6
GET /mul?a=6&b=7   -> 200  42
GET /div?a=9&b=3   -> 200  3

GET /div?a=1&b=0   -> 400  division by zero
GET /add?a=x&b=3   -> 400  parameter a is not an integer
GET /pow?a=2&b=8   -> 404  no such operation
POST /add          -> 405  (with Allow: GET)
GET /add  (no Host)-> 400  missing Host header
```

## Run it

```bash
python3 server.py          # port 8080
python3 server.py 8090     # or any port you like
```

Requires Python 3 and nothing else.

## Test it

```bash
python3 test_client.py     # or: python3 test_client.py 8090
```

`test_client.py` drives the server the way the marker will — one
`socket.create_connection`, every request down the same pipe — then checks the
socket is still open and that the client's buffer is empty, meaning the server
consumed exactly its own bytes and no more. It also covers pipelining, bodies,
chunked encoding and `Connection: close`.

```
one socket, sequential requests
PASS  GET /add?a=2&b=3  -> 200  5
...
PASS  socket still open
PASS  nothing over-read  b''
```

## Where one request ends and the next begins

HTTP/1.0 could answer this for free: the request ended when the socket did. Once
the connection is persistent that answer is gone, and it is the only genuinely
hard part of the exercise.

The whole defence is `Stream`, a buffered reader with one rule: never hand out a
byte that belongs to the next request.

- `read_line` scans the buffer for `\r\n` and slices only up to it. Whatever
  arrived in the same `recv` after that line stays in the buffer.
- `read_exactly(n)` refuses to return until it holds `n` bytes, then removes
  exactly `n`. A `recv` that dragged in the next request's first bytes is
  harmless — they are still sitting there when the loop comes round again.

So the connection loop is just: read a request line, read headers until a blank
one, read the body framed by `Content-Length` (or chunk-by-chunk if
`Transfer-Encoding: chunked`), answer, repeat. Pipelining falls out of this for
free: eight requests written in one `send` are eight passes through the same
loop, answered in order, because the buffer never lost its place.

Every response carries its own `Content-Length` too, so the client can play the
same game in the other direction.

## The rest of the behaviour

**Host.** HTTP/1.1 requires it; a request without one is a 400.

**Connection: close.** Honoured — the response echoes `Connection: close` and
the socket is shut down after it. HTTP/1.0 requests are closed by default unless
they ask for `keep-alive`. Everything else gets `Connection: keep-alive` and
stays up.

**Two clocks, not one.** A socket timeout only bounds the gap between two
packets, and a slow client can reset it forever by dribbling a byte every 29
seconds. So `Stream` keeps an absolute deadline instead: a connection may idle
30 seconds waiting for its next request (`Keep-Alive: timeout=30` advertises
it), but once the first byte of a request lands, the whole request has 10
seconds to arrive. Miss the second deadline and the answer is 408; miss the
first and the connection is simply closed, because a client that never started
talking has nothing to be told.

**Expect: 100-continue.** Honoured — the interim `100 Continue` goes out before
the body is read, so a client that waits for it is not left hanging. Any other
expectation is a 417.

**Errors keep the line open.** 400, 404 and 405 are answers, not accidents, so
the connection survives them. Only a stream the server can no longer trust — a
malformed request line, a bad chunk size, a `Content-Length` that isn't a
number — closes the connection, because at that point it has no idea where the
next request would start.

**One framing per request.** `Content-Length` and `Transfer-Encoding` arriving
together is a 400, not a preference to resolve. If the server picks one and a
proxy ahead of it picks the other, they disagree about where the request ends,
and the bytes in the gap become a request nobody authorised — the server would
happily answer a `GET` smuggled in behind a body. The same reasoning makes a
second `Host`, `Content-Length` or `Transfer-Encoding` header a 400 rather than
a comma-joined list: for headers that decide framing or routing, a duplicate is
an ambiguity, not a value.

**Limits.** 8 KB of headers, 1 MB of body and 64 concurrent connections,
answered with 431, 413 and 503. A persistent connection is held open by
definition, so without a ceiling a handful of idle clients would own every
thread.

**Closing.** `shutdown(SHUT_WR)` and then drain, rather than closing outright.
Slamming the socket shut while the peer still has bytes in flight makes the
kernel answer them with RST, which can throw away a response the client had not
read yet.

## Files

| | |
|---|---|
| `server.py` | the server; `Stream` does the framing, `handle` does the arithmetic |
| `test_client.py` | the marking run, plus pipelining, bodies and connection edges |
