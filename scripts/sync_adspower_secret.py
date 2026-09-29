#!/usr/bin/env python3
"""Copy ADS_API_KEY from deployment config to the Kubernetes Secret contract."""

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path

from social_crawler.config import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Explicit Kubernetes context")
    parser.add_argument("--namespace", default="social-crawler")
    args = parser.parse_args(argv)
    if not os.environ.get("CRAWLER_ENV_FILE") and Path(".env.deploy").is_file():
        os.environ["CRAWLER_ENV_FILE"] = str(Path(".env.deploy").resolve())
    load_config()
    key = os.environ.get("ADS_API_KEY", "").strip()
    if not key:
        parser.error("Set ADS_API_KEY in .env.deploy first")
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "adspower-api", "namespace": args.namespace},
        "type": "Opaque",
        "data": {"api-key": base64.b64encode(key.encode()).decode()},
    }
    # The key travels only over stdin, never in shell history, argv or stdout.
    # Server-side apply also avoids a last-applied annotation containing it.
    try:
        result = subprocess.run(
            ["kubectl", "--context", args.context, "--namespace", args.namespace,
             "--request-timeout=30s", "apply", "--server-side",
             "--field-manager=social-crawler-config", "-f", "-"],
            input=json.dumps(manifest), text=True, capture_output=True, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit("Could not run kubectl or the request timed out") from None
    if result.returncode:
        # Some kubectl errors include the request body. Keep it private.
        raise SystemExit(
            "Could not update adspower-api; check context, namespace, permissions and field ownership"
        )
    print("Updated Secret adspower-api. Restart the AdsPower Pod to use the new key.")


if __name__ == "__main__":
    main()
