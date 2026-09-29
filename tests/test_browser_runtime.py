from pathlib import Path

from social_crawler.environments.browser_runtime import (
    BROWSER_EXECUTABLE_PATH_ENV,
    CHROME_FOR_TESTING_VERSION,
    CHROME_MAJOR,
    CURL_CFFI_IMPERSONATE,
    DEFAULT_UA,
    browser_runtime_identity,
    playwright_launch_options,
)
from social_crawler.environments.environment_snapshot import browser_major


def test_pinned_chrome_identity_is_internally_consistent():
    assert CHROME_FOR_TESTING_VERSION.startswith(f"{CHROME_MAJOR}.")
    assert browser_major(CURL_CFFI_IMPERSONATE) == CHROME_MAJOR
    assert browser_major(DEFAULT_UA) == CHROME_MAJOR


def test_pinned_executable_is_selected_unless_channel_is_explicit(monkeypatch, tmp_path):
    executable = tmp_path / "chrome"
    monkeypatch.setenv(BROWSER_EXECUTABLE_PATH_ENV, str(executable))

    assert playwright_launch_options() == {"executable_path": str(executable)}
    assert playwright_launch_options("chrome") == {"channel": "chrome"}


def test_runtime_identity_checks_actual_binary_version(monkeypatch, tmp_path):
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\necho 'Google Chrome for Testing 150.0.7871.124'\n")
    executable.chmod(0o755)
    monkeypatch.setenv(BROWSER_EXECUTABLE_PATH_ENV, str(executable))

    identity = browser_runtime_identity()

    assert identity["executable_path"] == str(executable)
    assert identity["major"] == 150
    assert identity["aligned"] is True


def test_docker_pin_matches_application_identity():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()

    assert f"ARG CHROME_FOR_TESTING_VERSION={CHROME_FOR_TESTING_VERSION}" in dockerfile
    assert "CRAWLER_BROWSER_EXECUTABLE_PATH=/opt/chrome-for-testing/" in dockerfile
