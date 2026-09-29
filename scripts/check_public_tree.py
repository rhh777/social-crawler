#!/usr/bin/env python3
"""Check publishable working-tree files; report locations, never matched secrets.

Includes tracked files (even if ignored) and untracked, non-ignored additions.
Does not scan Git history, ignored runtime files, or binary image contents.
Optional --deny-file contains private, case-insensitive literal terms, one per line.
"""

import argparse
import ipaddress
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "api-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-(?:ant-)?[\w-]{20,})"),
    "personal-path": re.compile(r"/(?:Users|home)/(?!(?:crawler|user|username|example)(?:/|\b))[\w.-]+/"),
    "internal-host": re.compile(
        r"\b(?:[a-z0-9-]+\.)+(?:inner|corp|intranet)\.[a-z0-9.-]+\b"
        r"|\b[a-z0-9.-]+\.internal\b",
        re.I,
    ),
}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"\[([0-9a-fA-F]*:[0-9a-fA-F:]+)\]")
MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)")
PRIVATE_ROOTS = {
    ".private-backup", "data", "profiles", "browser_data", "logs", "artifacts",
    "social-crawler-local", "references", ".venv", "dist", "build",
}
EXAMPLE_NETS = tuple(ipaddress.ip_network(net) for net in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def public_files(root):
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({Path(name) for name in names.decode().split("\0") if name})


def private_path(path):
    return (
        path.parts[0] in PRIVATE_ROOTS
        or str(path).startswith("deploy/k8s/overlays/dev-private/")
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.name in {"config.local.toml", "agent-settings.json"}
        or path.suffix in {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"}
    )


def non_example_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback or address.is_unspecified
        or value == "255.255.255.255"
        or any(address in net for net in EXAMPLE_NETS if address.version == net.version)
    )


def scan(root, deny_terms=()):
    files = public_files(root)
    existing = {p for p in files if (root / p).is_file() or (root / p).is_symlink()}
    findings = []
    binary = []
    for relative in sorted(existing):
        path = root / relative
        if private_path(relative):
            findings.append((str(relative), 0, "private-file"))
        if path.is_symlink():
            findings.append((str(relative), 0, "symlink-review"))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            binary.append(str(relative))
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                # Docker's documented local host alias is portable, not a private DNS entry.
                checked = line.replace("host.docker.internal", "localhost")
                if pattern.search(checked):
                    findings.append((str(relative), number, label))
            if any(term.casefold() in line.casefold() for term in deny_terms):
                findings.append((str(relative), number, "private-term"))
            for match in IPV4.finditer(line):
                prefix = line[:match.start()]
                if re.search(r"(?:Chrome|Chromium|Version|Safari|Edg)/$", prefix):
                    continue
                if re.search(r"(?:^|[\s,(])(?:browser_)?version\s*=\s*['\"]?$", prefix, re.I):
                    continue
                if non_example_ip(match.group()):
                    findings.append((str(relative), number, "non-example-ip"))
            for match in IPV6.finditer(line):
                if non_example_ip(match.group(1)):
                    findings.append((str(relative), number, "non-example-ip"))
        if relative.suffix != ".md":
            continue
        # Ignore fenced code blocks; check local inline links against the public tree.
        prose = re.sub(r"^```[^\n]*\n.*?^```[^\n]*$", "", content, flags=re.M | re.S)
        for match in MARKDOWN_LINK.finditer(prose):
            url = urlsplit(match.group(1))
            if url.scheme or url.netloc or not url.path:
                continue
            target = (path.parent / unquote(url.path)).resolve()
            try:
                local = target.relative_to(root)
            except ValueError:
                local = None
            if local is None or not (
                local in existing
                or (target.is_dir() and any(local in p.parents for p in existing))
            ):
                findings.append((str(relative), 0, "non-public-document-link"))
    return sorted(set(findings)), len(existing), binary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--deny-file", type=Path)
    args = parser.parse_args(argv)
    terms = []
    if args.deny_file:
        terms = [line.strip() for line in args.deny_file.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    findings, count, binary = scan(args.root.resolve(), terms)
    for path, line, label in findings:
        print(f"{path}:{line}: {label}")
    print(f"Checked {count} public working-tree files; {len(findings)} finding(s).")
    if binary:
        print(f"Manual binary-content review required for {len(binary)} file(s):")
        for path in binary:
            print(f"  {path}")
    print("Scope: working tree only; no history, ignored files, or binary-content scan.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
