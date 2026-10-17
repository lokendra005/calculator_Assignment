#!/bin/sh
# A user journey, not just a liveness check: real arithmetic, a real error,
# and the version actually serving it.  Usage: smoke.sh BASE_URL [EXPECTED_SHA]
set -eu
base=$1
test "$(curl -fsS "$base/add?a=2&b=3")" = 5
test "$(curl -fsS "$base/div?a=84&b=2")" = 42
test "$(curl -sS -o /dev/null -w '%{http_code}' "$base/div?a=1&b=0")" = 400
version=$(curl -fsS "$base/version")
if [ -n "${2:-}" ] && [ "$version" != "$2" ]; then
  echo "smoke FAILED: serving $version, expected $2" >&2
  exit 1
fi
echo "smoke ok: $base serves $version"
