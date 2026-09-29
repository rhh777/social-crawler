"""Run isolated PostgreSQL, without Docker or installing a system service.

uv run --no-project --python 3.11 --with pgserver==0.1.4 scripts/local_postgres.py
pgserver's binary wheel is available for Python 3.11; the application uses 3.13.
"""

import argparse
import signal
import threading
from pathlib import Path

import pgserver

p = argparse.ArgumentParser()
p.add_argument("--directory", default="data/postgres")
p.add_argument("--url-file", default="data/postgres.url")
args = p.parse_args()
directory = Path(args.directory).resolve()
directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
server = pgserver.get_server(directory, cleanup_mode="stop")
url_file = Path(args.url_file)
url_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
url_file.write_text(server.get_uri())
url_file.chmod(0o600)
stopped = threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stopped.set())
print(
    f"PostgreSQL ready; connection URI saved to {url_file}. Ctrl-C stops this local server.",
    flush=True,
)
stopped.wait()
server.cleanup()
