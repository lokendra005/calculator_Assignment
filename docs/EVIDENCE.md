# Evidence

Everything here comes from real runs on 2026-10-18: GitHub Actions, and a
kind cluster on my laptop (Docker Desktop, 4 GB). Raw output is in
[`evidence/`](evidence/).

**Version under test:** `a7e33ef4d8bef2745f88303b8aa6c7e376a26d6b`. This is the
last commit that changes application code, the chart, or CI. Later commits
change only docs, evidence, and `deploy/monitoring.yaml` (the monitoring
stack's values, which do not affect the app image). The PR description gives
the final commit and its CI run.

## 1. Source → CI → registry → cluster, linked by sha

| Step | Evidence |
|---|---|
| CI run for `a7e33ef` | [run 39080505324](https://github.com/lokendra005/calculator_Assignment/actions/runs/39080505324): `test` ✓, `config` ✓, `release` ✓ |
| What CI ran | 39 protocol and ops checks ("all green"), helm lint, kubeconform (5 valid, 2 CRDs skipped), Trivy config scan, Trivy image scan, deploy to kind, `smoke ok … serves a7e33ef…`, push |
| Image in GHCR | `ghcr.io/lokendra005/calculator:a7e33ef4d8bef2745f88303b8aa6c7e376a26d6b`, digest `sha256:4f403675d7ac7a761202f56b9d8a08c7b9c4f296af95f1a443690cd7d392457f` |
| Pulled for deploy | `make deploy` printed the same digest, `sha256:4f403675…457f`. The same check passed for the earlier commit `842caba` (`sha256:2f1e38c2…5a17` in both CI and the local pull). |
| Running version | `curl localhost:30080/version` returns `a7e33ef4…`. `calc_build_info{version="a7e33ef4…"}` reports it on both pods ([prometheus.txt](evidence/prometheus.txt)), and every log line carries `"version": "a7e33ef4…"`. |

## 2. A working user journey

`deploy/smoke.sh` runs at the end of every CI run and every `make deploy`.
It checks `2+3=5`, `84/2=42`, `1/0 → 400`, and that `/version` is the expected sha.

```
smoke ok: http://localhost:30080 serves a7e33ef4d8bef2745f88303b8aa6c7e376a26d6b
```

A single request followed through the system: the caller's W3C trace id comes
back in `traceresponse`, and the same id appears in the pod's JSON log line
([request-trace-log.txt](evidence/request-trace-log.txt)):

```
traceresponse: 00-4bf92f3577b34da6a3ce929d0e0e4736-b90db27860893bda-01
42
{"msg": "request", "version": "a7e33ef4…", "op": "/mul", "status": 200, "ms": 0.616, "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", ...}
```

## 3. Release control

Each drill ran with `deploy/load.py` sending about 15 requests per second, each one checked for the correct answer.

| Drill | Result | Raw |
|---|---|---|
| Rolling restart (`maxUnavailable: 0`, drain on SIGTERM) | **702 ok, 0 failed** | [rollout-load.txt](evidence/rollout-load.txt) |
| Bad release: `image.tag=does-not-exist` with `--rollback-on-failure` | Helm marked revision 2 `failed` and created revision 3, "Rollback to 1". **1696 ok, 0 failed.** The old pods kept serving the whole time. | [bad-release-helm.txt](evidence/bad-release-helm.txt), [bad-release-history.txt](evidence/bad-release-history.txt), [bad-release-load.txt](evidence/bad-release-load.txt) |
| Force-kill one pod | The ReplicaSet replaced it in seconds. **617 ok, 0 failed.** | [pod-kill.txt](evidence/pod-kill.txt), [pod-kill-load.txt](evidence/pod-kill-load.txt) |
| Drift | `make drift` prints "no drift" after the drills. An earlier manual `kubectl scale --replicas=3` was detected (`-replicas: 3 / +replicas: 2`, exit 1). | in the terminal session |

## 4. Observability and reliability: a drill that found a bug

The saturation drill held about 150 idle keep-alive connections against
2 pods × 64 slots for 7 minutes, with load running alongside.

**Before** (`842caba`, probes on the public port), from [saturation-before-fix.txt](evidence/saturation-before-fix.txt):
- `CalculatorErrorBudgetBurn` went to pending and then **fired** (5xx ratio 14–28%). This is what the SLO alert is for.
- The kubelet events showed `Liveness probe failed: HTTP probe failed with statuscode: 503`, and then `failed liveness probe, will be restarted`.
  There were **8 restarts** of pods that were healthy, only busy.
- The users saw 680 of 4530 requests fail: 482 got a 503, **195 got connection resets** (from the restarts), and 3 timed out.

**Fix** (`a7e33ef`): probes and metrics moved to admin port 8081, with their own
connection budget. A PodMonitor scrapes it, and the NetworkPolicy admits only
the `monitoring` namespace. A regression test in `test_client.py` fills port
8080 and asserts that 8081 still answers.

**After** (`a7e33ef`), from [saturation-after-fix.txt](evidence/saturation-after-fix.txt):
- **0 restarts, 0 connection resets, and no probe failures during the drill.** The only two probe events are from pod startup, before the drill began.
- The alert still fired, which is correct: users really were getting 503s.
- `max_over_time(calc_open_connections[10m])` was 64 on both pods, so the ceiling held exactly.
- The recovery is recorded at the end of the file: the alert resolves, the error ratio falls back, and `smoke.sh` passes.

**What the fix made visible.** Without restarts freeing slots now and then,
the idle hog kept all 128 slots for the whole drill, and 98% of real requests
got a 503. A per-pod connection ceiling with a 30-second idle timeout lets one
client take all the capacity. The next change would be to close the
longest-idle keep-alive connection when a new one arrives at the ceiling,
instead of refusing the newcomer, and to add per-client limits at an ingress.
I recorded this rather than rushing the change in.

**A second, unplanned fault.** With a 2 GB Docker VM, a monitoring upgrade
briefly ran two Grafana pods, and the node logged `SystemOOM … victim process: grafana`.
After that, every pod's probes timed out, including CoreDNS and the scheduler.
Symptom: cluster-wide probe failures. Evidence: the node OOM event and
`docker stats` at 1.75 of 1.93 GiB. The changes:
- Grafana now uses the `Recreate` strategy.
- The monitoring components have memory limits.
- The VM has 4 GB, and the README lists 4 GB as a prerequisite.

Verification: the fresh cluster settled at 2.1 of 3.8 GiB, and every drill
above ran on it.

## 5. Security checks at runtime

From [security.txt](evidence/security.txt) and [netpol-admin-port.txt](evidence/netpol-admin-port.txt), rechecked on `a7e33ef`:

```
uid 65532 gid 65532
root fs read-only: Read-only file system
1.1.1.1 blocked: TimeoutError timed out                 # egress denied
example.com blocked: gaierror ... name resolution       # not even DNS
service account token present: False
exec: "sh": executable file not found in $PATH          # distroless
default ns -> :8080/version = a7e33ef4…                 # public port open
default ns -> :8081/healthz = blocked (timeout)         # admin port: monitoring ns only
```

CI fails the build on fixable HIGH or CRITICAL CVEs. That gate rejected the
first base image I chose (distroless Debian 12, 43 fixable HIGH findings),
and I moved to Debian 13.

## Limitations of this evidence

- There are no screenshots. The Grafana dashboard "Calculator" is provisioned
  from `chart/calculator/dashboards/calculator.json`, which the Grafana API
  confirms (`/api/search` → `uid: calculator`). Open it with `make grafana`.
- Single node: node failure was not drilled because it would be a full outage by construction.
- The alerts are evaluated in Prometheus but not routed, since there is no Alertmanager.
