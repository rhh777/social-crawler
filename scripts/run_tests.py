"""Run automated tests; --postgres enables database integration tests."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--postgres", action="store_true")
parser.add_argument("--database-url-file", default="data/postgres.url")
args = parser.parse_args()
env = os.environ.copy()
if args.postgres:
    env["CRAWLER_TEST_DATABASE_URL"] = (
        env.get("CRAWLER_TEST_DATABASE_URL") or Path(args.database_url_file).read_text().strip()
    )
Path("data").mkdir(exist_ok=True)
raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--junitxml=data/test-results.xml"], env=env
    ).returncode
)
