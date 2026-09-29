#!/bin/sh
set -eu

: "${ADS_API_KEY:?ADS_API_KEY must be supplied through a runtime secret}"

ready_file="${ADS_READY_FILE:-/tmp/adspower-ready}"
rm -f "$ready_file"

stop_runtime() {
  ads close-all-profiles >/dev/null 2>&1 || true
  ads stop >/dev/null 2>&1 || true
}

trap 'stop_runtime; exit 0' INT TERM

ads start -k "$ADS_API_KEY"

attempt=0
until ads check-status --port "${PORT}" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "AdsPower Local API did not become ready" >&2
    exit 1
  fi
  sleep 1
done

echo "AdsPower Local API is ready on 127.0.0.1:${PORT}"

if [ -n "${ADS_KERNEL_VERSION:-}" ]; then
  # Do not use Node here. The AdsPower CLI injects a preload module into Node
  # processes, which can keep the event loop alive after this poll completes.
  python <<'PY'
import json
import os
import time
import urllib.request

port = os.environ.get("PORT", "50325")
version = os.environ["ADS_KERNEL_VERSION"]
base = f"http://127.0.0.1:{port}"


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.load(response)
    if not payload or payload.get("code") != 0:
        raise RuntimeError("AdsPower kernel operation failed")
    return payload.get("data") or {}


request(
    "/api/v2/browser-profile/download-kernel",
    {"kernel_type": "Chrome", "kernel_version": version},
)
for _ in range(120):
    data = request("/api/v2/browser-profile/kernels?kernel_type=Chrome")
    item = next(
        (row for row in data.get("list", []) if str(row.get("kernel")) == version),
        None,
    )
    if item and item.get("is_downloaded"):
        print(f"AdsPower Chrome {version} kernel is ready", flush=True)
        break
    time.sleep(5)
else:
    raise RuntimeError("AdsPower kernel download timed out")
PY
fi

touch "$ready_file"

failures=0
failure_limit="${ADS_HEALTH_FAILURES:-18}"
case "$failure_limit" in
  ''|*[!0-9]*) echo "ADS_HEALTH_FAILURES must be a positive integer" >&2; exit 1 ;;
esac
[ "$failure_limit" -gt 0 ] || {
  echo "ADS_HEALTH_FAILURES must be a positive integer" >&2
  exit 1
}
while sleep 10; do
  if adspower-healthy; then
    failures=0
    continue
  fi
  failures=$((failures + 1))
  # A busy headful browser can delay one reply; restarting would kill it.
  if [ "$failures" -ge "$failure_limit" ]; then
    echo "AdsPower Local API stopped responding" >&2
    exit 1
  fi
done
