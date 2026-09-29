from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

CHROME_MAJOR = 150
CHROME_FOR_TESTING_VERSION = "150.0.7871.124"
CURL_CFFI_IMPERSONATE = f"chrome{CHROME_MAJOR}"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
BROWSER_EXECUTABLE_PATH_ENV = "CRAWLER_BROWSER_EXECUTABLE_PATH"


def playwright_launch_options(browser_channel: str | None = None) -> dict[str, str]:
    """Select one browser binary without mixing channel and executable overrides."""

    if browser_channel:
        return {"channel": browser_channel}
    executable = os.environ.get(BROWSER_EXECUTABLE_PATH_ENV, "").strip()
    return {"executable_path": executable} if executable else {}


def browser_runtime_identity() -> dict[str, Any]:
    """Report the pinned identity without starting a browser or contacting a site."""

    executable = os.environ.get(BROWSER_EXECUTABLE_PATH_ENV, "").strip()
    result: dict[str, Any] = {
        "expected_major": CHROME_MAJOR,
        "chrome_for_testing_version": CHROME_FOR_TESTING_VERSION,
        "curl_cffi_impersonate": CURL_CFFI_IMPERSONATE,
        "executable_path": executable or None,
        "executable_present": bool(executable and Path(executable).is_file()),
    }
    if not result["executable_present"]:
        result["version"] = None
        result["major"] = None
        result["aligned"] = None if executable == "" else False
        return result
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = completed.stdout.strip() or completed.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(version=None, major=None, aligned=False, error=type(exc).__name__)
        return result
    found = re.search(r"(?:Chrome|Chromium)(?: for Testing)?\s+(\d+)", version)
    major = int(found.group(1)) if found else None
    result.update(version=version, major=major, aligned=major == CHROME_MAJOR)
    return result
