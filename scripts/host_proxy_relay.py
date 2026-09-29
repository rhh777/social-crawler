#!/usr/bin/env python3
"""Run native TCP relays for container proxy traffic on macOS/OrbStack."""

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from social_crawler.config import load_config


def dotenv(name: str) -> str:
    if not os.environ.get("CRAWLER_ENV_FILE") and Path(".env.deploy").is_file():
        os.environ["CRAWLER_ENV_FILE"] = str(Path(".env.deploy").resolve())
    load_config()
    return os.environ.get(name, "").strip()


def routes(value: str) -> list[tuple[str, int, int]]:
    result = []
    for item in value.split(","):
        if not item.strip():
            continue
        try:
            upstream, listen_text = item.strip().rsplit("=", 1)
            host, port_text = upstream.rsplit(":", 1)
            port, listen = int(port_text), int(listen_text)
        except ValueError:
            raise SystemExit(f"Invalid relay route: {item}") from None
        if not host or not 1 <= port <= 65535 or not 1 <= listen <= 65535:
            raise SystemExit(f"Invalid relay route: {item}")
        result.append((host, port, listen))
    if not result:
        raise SystemExit("No routes configured in CRAWLER_PROXY_RELAY_MAP")
    if len({listen for _, _, listen in result}) != len(result):
        raise SystemExit("Each relay route must use a unique local port")
    return result


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (OSError, ConnectionError):
        pass
    finally:
        writer.close()


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_host: str,
    upstream_port: int,
) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_host, upstream_port), timeout=15
        )
    except (OSError, TimeoutError):
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, upstream_writer),
        pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def serve(route_list: list[tuple[str, int, int]]) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    servers = []
    for upstream_host, upstream_port, listen_port in route_list:
        server = await asyncio.start_server(
            lambda reader, writer, host=upstream_host, port=upstream_port: handle(
                reader, writer, host, port
            ),
            "127.0.0.1",
            listen_port,
        )
        servers.append(server)
        print(
            f"127.0.0.1:{listen_port} -> {upstream_host}:{upstream_port}",
            flush=True,
        )
    try:
        await stop.wait()
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))


def live_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8"))
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(args, route_list: list[tuple[str, int, int]]) -> None:
    if pid := live_pid(args.pid_file):
        print(f"Host proxy relay is already running (pid {pid})")
        return
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--mapping",
        args.mapping,
        "--pid-file",
        str(args.pid_file),
        "--log-file",
        str(args.log_file),
    ]
    with args.log_file.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    args.pid_file.write_text(str(process.pid), encoding="utf-8")
    for _ in range(30):
        if process.poll() is not None:
            args.pid_file.unlink(missing_ok=True)
            raise SystemExit(f"Host proxy relay failed; inspect {args.log_file}")
        if all(_port_open(port) for _, _, port in route_list):
            print(f"Host proxy relay started (pid {process.pid})")
            return
        time.sleep(0.1)
    process.terminate()
    args.pid_file.unlink(missing_ok=True)
    raise SystemExit("Host proxy relay did not become ready")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def stop(pid_file: Path) -> None:
    pid = live_pid(pid_file)
    if not pid:
        pid_file.unlink(missing_ok=True)
        print("Host proxy relay is not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            pid_file.unlink(missing_ok=True)
            print("Host proxy relay stopped")
            return
        time.sleep(0.1)
    raise SystemExit(f"Host proxy relay did not stop (pid {pid})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "start", "stop", "status"), nargs="?", default="run")
    parser.add_argument("--mapping", default=dotenv("CRAWLER_PROXY_RELAY_MAP"))
    parser.add_argument("--pid-file", type=Path, default=Path("data/host-proxy-relay.pid"))
    parser.add_argument("--log-file", type=Path, default=Path("logs/host-proxy-relay.log"))
    args = parser.parse_args()
    if args.action == "stop":
        stop(args.pid_file)
        return
    if args.action == "status":
        pid = live_pid(args.pid_file)
        print(f"running (pid {pid})" if pid else "stopped")
        raise SystemExit(0 if pid else 1)
    route_list = routes(args.mapping)
    if args.action == "start":
        start(args, route_list)
    else:
        asyncio.run(serve(route_list))


if __name__ == "__main__":
    main()
