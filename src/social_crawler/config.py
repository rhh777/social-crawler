"""Load user configuration at process entry points, never at import time."""

import argparse
import os
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream


def load_config() -> Path | None:
    """Process environment wins; an explicitly selected missing file is an error.

    Do not expand variables or execute shell syntax in secrets. Entry points call
    this before reading defaults; importing models in tests has no side effects.
    """
    selected = os.environ.get("CRAWLER_ENV_FILE")
    path = Path(selected).expanduser() if selected else Path.cwd() / ".env"
    if not path.is_file():
        if selected:
            raise ValueError(f"CRAWLER_ENV_FILE does not exist: {path}")
        return None
    with path.open(encoding="utf-8") as stream:
        for binding in parse_stream(stream):
            if binding.error:
                # Never echo a malformed line: it may contain a credential.
                raise ValueError(f"Invalid .env syntax at {path}:{binding.original.line}")
    for key, value in dotenv_values(path, interpolate=False).items():
        if value is not None:
            os.environ.setdefault(key, value)
    return path.resolve()


def main():
    parser = argparse.ArgumentParser(description="Load .env and run a command")
    parser.add_argument("--exec", dest="command", nargs=argparse.REMAINDER, required=True)
    args = parser.parse_args()
    if not args.command:
        parser.error("--exec requires a command")
    try:
        path = load_config()
    except ValueError as exc:
        parser.error(str(exc))
    if path:
        # Child launchers may change cwd before invoking another entry point.
        os.environ["CRAWLER_ENV_FILE"] = str(path)
    os.execvpe(args.command[0], args.command, os.environ)


if __name__ == "__main__":
    main()
