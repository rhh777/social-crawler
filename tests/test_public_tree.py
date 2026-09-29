"""The publication check must see new leaks without reading private runtime data."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_public_tree", Path(__file__).resolve().parents[1] / "scripts/check_public_tree.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_scans_tracked_ignored_files_and_untracked_additions(repo):
    (repo / "data").mkdir()
    (repo / "data/private.txt").write_text("not-a-real-credential")
    subprocess.run(["git", "-C", str(repo), "add", "data/private.txt"], check=True)
    (repo / ".gitignore").write_text("data/\n")
    (repo / "data/ignored.txt").write_text("PRIVATE_ORGANIZATION")
    (repo / "new.md").write_text("PRIVATE_ORGANIZATION")
    findings, _, _ = CHECK.scan(repo, ["PRIVATE_ORGANIZATION"])
    assert findings == [
        ("data/private.txt", 0, "private-file"),
        ("new.md", 1, "private-term"),
    ]


def test_document_links_must_be_publishable(repo):
    (repo / ".gitignore").write_text("private/\n")
    (repo / "private").mkdir()
    (repo / "private/notes.md").write_text("local notes")
    (repo / "README.md").write_text("[notes](private/notes.md)\n[guide](docs/guide.md)")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("[home](../README.md)")
    findings, _, _ = CHECK.scan(repo)
    assert findings == [("README.md", 0, "non-public-document-link")]


def test_example_addresses_and_browser_versions_are_allowed():
    for address in ("127.0.0.1", "0.0.0.0", "192.0.2.10", "203.0.113.7", "2001:db8::1"):
        assert not CHECK.non_example_ip(address)
    # Construct a non-example address without embedding an infrastructure endpoint.
    assert CHECK.non_example_ip("10." + "20.30.40")


def test_browser_version_assignment_is_not_an_ip_address(repo):
    (repo / "test_runtime.py").write_text(
        'browser = SimpleNamespace(version="151.0.0.0")\n'
        'upstream = "' + "10." + '20.30.40"\n'
    )
    findings, _, _ = CHECK.scan(repo)
    assert findings == [("test_runtime.py", 2, "non-example-ip")]


def test_address_findings_do_not_echo_value(repo, capsys):
    (repo / "config.txt").write_text("upstream=" + "10." + "20.30.40\n")
    assert CHECK.main(["--root", str(repo)]) == 1
    output = capsys.readouterr().out
    assert "config.txt:1: non-example-ip" in output
    assert "20.30.40" not in output


def test_ignored_directory_link_and_external_symlink_are_rejected(repo):
    (repo / ".gitignore").write_text("private/\n")
    (repo / "private").mkdir()
    (repo / "README.md").write_text("[archive](private/)\n[docs](docs/)")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("guide")
    (repo / "secret-link").symlink_to(repo.parent / "outside-file")
    findings, _, _ = CHECK.scan(repo)
    assert findings == [
        ("README.md", 0, "non-public-document-link"),
        ("secret-link", 0, "symlink-review"),
    ]
