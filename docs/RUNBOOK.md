# Runbook

First look for any alert: `make prometheus`, then open http://localhost:9090/alerts.
The dashboard: `make grafana`, then open http://localhost:3000 → Calculator.

## CalculatorDown

**Meaning:** Prometheus is scraping no calculator pod, so users are almost
certainly failing too.

1. `kubectl -n calculator get pods`: are the pods Pending, CrashLoopBackOff or ImagePullBackOff?
2. `kubectl -n calculator describe pod <pod>`: the events show the reason (bad image tag, OOMKilled, probe failures).
3. `kubectl -n calculator logs <pod> --previous`: the last JSON log lines before the crash.
4. If it started with a release: `make rollback`.
5. Verify: `make smoke` passes, and `up{service="calculator"}` is back to 2.

## CalculatorDegraded

**Meaning:** fewer replicas are available than desired. The service still
works, but one more failure would be an outage.

1. `kubectl -n calculator get pods -o wide` and `kubectl -n calculator get events --sort-by=.lastTimestamp`.
2. A Pending pod usually means not enough resources on the node. Check `kubectl describe node`.
3. Restarts are visible on the "Pod restarts" dashboard panel. Check the logs of the restarting pod.

## CalculatorErrorBudgetBurn

**Meaning:** more than 1% of arithmetic requests returned 5xx over 5 minutes.
The only 5xx this server produces is the 503 for the connection ceiling
(`op="refused"`), so this alert means the pods are saturated.

1. On the dashboard, check whether "Open connections per pod" is near 64.
2. `kubectl -n calculator logs -l app.kubernetes.io/name=calculator | grep refused`: which peers are being refused.
3. Short term: `kubectl -n calculator scale deploy/calculator --replicas=4`. That
   is deliberate drift, which `make drift` will show; then record it in `values.yaml`.
4. If one client is holding idle connections, that client is the problem. `IDLE_TIMEOUT` (30 s) frees them eventually.

## CalculatorSlow

**Meaning:** p95 latency of arithmetic requests is above 100 ms for 10 minutes.
Normal p95 is under 2.5 ms.

1. Check whether it started with a release (`make status`), and roll back if so.
2. Check CPU on the node: `kubectl top` needs metrics-server, which is not
   installed here. Instead, use the kube-state-metrics panels in Grafana.

## Drill: bad release and recovery

This drill was run on the local cluster; its output is in [EVIDENCE.md](EVIDENCE.md).

- **Symptom:** a release whose image cannot start, for example a non-existent tag.
- **Hypothesis:** `maxUnavailable: 0` keeps the old pods serving while the new
  pod never becomes Ready, and `--rollback-on-failure` reverts the release
  after the timeout.
- **Test:**
  1. Start load: `python3 deploy/load.py 150`.
  2. In parallel: `helm upgrade calculator chart/calculator -n calculator --reuse-values --set image.tag=does-not-exist --rollback-on-failure --timeout 60s`.
- **Evidence:** load.py reports 0 failures. `helm history` shows the failed
  revision followed by an automatic rollback revision. `/version` never changes.
- **Verify recovery:** `make status` shows the original sha, `make smoke` passes, and `make drift` reports no drift.

## Drill: pod failure

1. Start load: `python3 deploy/load.py 60`.
2. Kill a pod: `kubectl -n calculator delete pod <one pod> --grace-period=0 --force`.
3. Expect no failures, or at most a few connection resets for requests
   already in flight on the killed pod. A forced kill skips the graceful
   drain, which is the point of the drill. The ReplicaSet recreates the pod
   within seconds.
