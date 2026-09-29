import asyncio
from urllib.parse import unquote, urlsplit


def direct_playwright_proxy(value: str) -> dict | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("Proxy must include a scheme and hostname")
    # Chromium accepts socks5 and sends domain names through that proxy. The
    # curl-specific socks5h spelling is normalized only for Playwright.
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy = {"server": server}
    if parsed.username:
        proxy.update(
            username=unquote(parsed.username),
            password=unquote(parsed.password or ""),
        )
    return proxy


class AuthenticatedSocks5Bridge:
    """Expose an unauthenticated loopback SOCKS5 endpoint for Chromium."""

    def __init__(self, upstream_url: str):
        parsed = urlsplit(upstream_url)
        if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname:
            raise ValueError("Authenticated SOCKS bridge requires a socks5 proxy URL")
        if not parsed.username:
            raise ValueError("Authenticated SOCKS bridge requires a username")
        self.host = parsed.hostname
        self.port = parsed.port or 1080
        self.username = unquote(parsed.username).encode()
        self.password = unquote(parsed.password or "").encode()
        if len(self.username) > 255 or len(self.password) > 255:
            raise ValueError("SOCKS5 username and password must be at most 255 bytes")
        self.server = None
        self.connections = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    @property
    def playwright_config(self) -> dict:
        if not self.server or not self.server.sockets:
            raise RuntimeError("SOCKS5 bridge has not started")
        port = self.server.sockets[0].getsockname()[1]
        return {"server": f"socks5://127.0.0.1:{port}"}

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        connections, self.connections = self.connections, set()
        for writer in connections:
            writer.close()
        if connections:
            await asyncio.gather(
                *(writer.wait_closed() for writer in connections), return_exceptions=True
            )

    async def _upstream_handshake(self, reader, writer, request: bytes) -> bytes:
        writer.write(b"\x05\x01\x02")
        await writer.drain()
        if await reader.readexactly(2) != b"\x05\x02":
            raise ConnectionError("Upstream SOCKS5 proxy rejected authentication method")
        writer.write(
            b"\x01"
            + bytes([len(self.username)])
            + self.username
            + bytes([len(self.password)])
            + self.password
        )
        await writer.drain()
        auth_response = await reader.readexactly(2)
        if auth_response != b"\x01\x00":
            raise ConnectionError("Upstream SOCKS5 proxy rejected credentials")
        writer.write(request)
        await writer.drain()
        head = await reader.readexactly(4)
        address = await _read_socks_address(reader, head[3])
        port = await reader.readexactly(2)
        return head + address + port

    async def _handle(self, client_reader, client_writer) -> None:
        upstream_writer = None
        replied = False
        self.connections.add(client_writer)
        try:
            version, method_count = await client_reader.readexactly(2)
            methods = await client_reader.readexactly(method_count)
            if version != 5 or 0 not in methods:
                client_writer.write(b"\x05\xff")
                await client_writer.drain()
                return
            client_writer.write(b"\x05\x00")
            await client_writer.drain()

            head = await client_reader.readexactly(4)
            if head[:3] != b"\x05\x01\x00":
                client_writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await client_writer.drain()
                replied = True
                return
            address = await _read_socks_address(client_reader, head[3])
            port = await client_reader.readexactly(2)
            request = head + address + port

            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=15
            )
            self.connections.add(upstream_writer)
            response = await self._upstream_handshake(
                upstream_reader, upstream_writer, request
            )
            client_writer.write(response)
            await client_writer.drain()
            replied = True
            if response[1] != 0:
                return

            client_to_upstream = asyncio.create_task(
                _relay(client_reader, upstream_writer)
            )
            upstream_to_client = asyncio.create_task(
                _relay(upstream_reader, client_writer)
            )
            done, pending = await asyncio.wait(
                {client_to_upstream, upstream_to_client},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except (OSError, asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            if not replied and not client_writer.is_closing():
                client_writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                try:
                    await client_writer.drain()
                except OSError:
                    pass
        finally:
            for writer in (upstream_writer, client_writer):
                if writer:
                    self.connections.discard(writer)
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass


async def _read_socks_address(reader, address_type: int) -> bytes:
    if address_type == 1:
        return await reader.readexactly(4)
    if address_type == 4:
        return await reader.readexactly(16)
    if address_type == 3:
        length = await reader.readexactly(1)
        return length + await reader.readexactly(length[0])
    raise ConnectionError("Unsupported SOCKS5 address type")


async def _relay(reader, writer) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()


async def prepare_playwright_proxy(
    value: str,
) -> tuple[dict | None, AuthenticatedSocks5Bridge | None]:
    direct = direct_playwright_proxy(value)
    if not direct:
        return None, None
    parsed = urlsplit(value)
    if parsed.scheme in {"socks5", "socks5h"} and parsed.username:
        bridge = AuthenticatedSocks5Bridge(value)
        await bridge.start()
        return bridge.playwright_config, bridge
    return direct, None
