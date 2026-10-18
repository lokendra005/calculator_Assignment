#!/usr/bin/env python3
"""Steady user traffic for rollout and failure drills; reports every failure.

    python3 deploy/load.py [seconds] [url]
"""
import sys
import time
import urllib.request

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:30080"
ok = failed = 0
versions = {}
end = time.time() + seconds
while time.time() < end:
    try:
        with urllib.request.urlopen(f"{url}/add?a=2&b=3", timeout=2) as r:
            assert r.read().strip() == b"5"
        with urllib.request.urlopen(f"{url}/version", timeout=2) as r:
            v = r.read().decode().strip()[:12]
        versions[v] = versions.get(v, 0) + 1
        ok += 1
    except Exception as e:
        failed += 1
        print(f"{time.strftime('%H:%M:%S')} FAIL {type(e).__name__}: {e}", flush=True)
    time.sleep(0.05)
print(f"ok={ok} failed={failed} versions_seen={versions}")
sys.exit(1 if failed else 0)
