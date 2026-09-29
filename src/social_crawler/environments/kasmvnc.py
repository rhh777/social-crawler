"""Private, per-session X displays using the unmodified KasmVNC client."""

import base64
import fcntl
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class KasmDesktop:
    _allocation_lock = threading.Lock()

    def __init__(self):
        self.port = None
        self.display = None
        self.processes = []
        self.directory = None
        self.lease = None
        self.password = secrets.token_urlsafe(32)

    @staticmethod
    def available():
        return all(shutil.which(name) for name in ('Xvnc', 'kasmvncpasswd', 'openbox'))

    @property
    def authorization(self):
        return 'Basic ' + base64.b64encode(('account:' + self.password).encode()).decode()

    @property
    def environment(self):
        return dict(os.environ, DISPLAY=self.display)

    def start(self):
        try:
            self.directory = tempfile.TemporaryDirectory(prefix='crawler-kasm-')
            directory = Path(self.directory.name)
            password_file = directory / 'passwd'
            subprocess.run(
                ['kasmvncpasswd', '-u', 'account', '-w', str(password_file)],
                input=f'{self.password}\n{self.password}\n', text=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10,
            )
            with self._allocation_lock:
                for number in range(100, 300):
                    lease = open(f'/tmp/crawler-kasm-display-{number}.lock', 'a')
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lease.close()
                        continue
                    if Path(f'/tmp/.X11-unix/X{number}').exists():
                        lease.close()
                        continue
                    self.lease, self.display = lease, f':{number}'
                    break
                else:
                    raise RuntimeError('No free browser displays')
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    self.port = reservation.getsockname()[1]
                # Authentication remains on the HTTP/WebSocket listener. Legacy TCP
                # RFB and TCP X11 are disabled. Shared X11 sockets support AdsPower.
                self.processes.append(subprocess.Popen([
                    'Xvnc', self.display, '-geometry', '1440x900', '-depth', '24',
                    '-interface', '127.0.0.1', '-websocketPort', str(self.port),
                    '-PublicIP', '127.0.0.1', '-httpd', '/usr/share/kasmvnc/www',
                    '-FrameRate', '30', '-KasmPasswordFile', str(password_file),
                    '-rfbport', '-1', '-SecurityTypes', 'None', '-nolisten', 'tcp', '-ac',
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                for _ in range(100):
                    if self.processes[0].poll() is not None:
                        raise RuntimeError('KasmVNC exited during startup')
                    try:
                        with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('KasmVNC startup timed out')
            self.processes.append(subprocess.Popen(
                ['openbox', '--sm-disable'], env=self.environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self.processes.clear()
        if self.directory:
            self.directory.cleanup()
        if self.lease:
            self.lease.close()
            self.lease = None
