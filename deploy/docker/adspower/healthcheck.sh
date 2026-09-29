#!/bin/sh
# Cheap Local API check for probes and the entrypoint watchdog. `ads
# check-status` starts a Node CLI (~0.75s idle) and exceeds probe timeouts
# while a headful browser saturates the CPU, restarting the sidecar mid-run.
# Use the base image's Python instead of assuming curl or wget is installed.
exec python - <<'PY'
import json
import os
import urllib.request

port = os.environ.get("PORT", "50325")
timeout = float(os.environ.get("ADS_HEALTH_TIMEOUT", "5"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout) as response:
    payload = json.load(response)
if payload.get("code") != 0:
    raise SystemExit(1)
PY
