#!/bin/sh
set -eu
if command -v Xvnc >/dev/null && command -v kasmvncpasswd >/dev/null && command -v openbox >/dev/null; then
    exit 0
fi
python - <<'PY'
import hashlib
from pathlib import Path
from urllib.request import urlopen

url = 'https://github.com/kasmtech/KasmVNC/releases/download/v1.5.0/kasmvncserver_bookworm_1.5.0_amd64.deb'
with urlopen(url, timeout=120) as response:
    data = response.read()
if hashlib.sha256(data).hexdigest() != '770fd3df51510beecc89666879d82faf411276e68c6e11df612f736b891b5f71':
    raise SystemExit('KasmVNC checksum mismatch')
Path('/tmp/kasmvnc.deb').write_bytes(data)
PY
apt-get update
apt-get install -y --no-install-recommends /tmp/kasmvnc.deb openbox x11-utils
rm -rf /var/lib/apt/lists/* /tmp/kasmvnc.deb
