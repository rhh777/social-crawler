"""Same-origin gateway for private KasmVNC sessions, including WebSockets."""

import http.client
import secrets
import select
import socket
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


def proxy_browser(handler, session, suffix):
    cookies = SimpleCookie()
    try:
        cookies.load(handler.headers.get('Cookie', ''))
        supplied = cookies.get('crawler_browser')
        authorized = supplied and secrets.compare_digest(supplied.value, session.access_token)
    except Exception:
        authorized = False
    if not authorized:
        handler.respond(403, {'error': '请从账号列表重新打开浏览器'})
        return
    if not session.active or not session.desktop or session.state not in {'opening', 'open', 'saving'}:
        handler.respond(410, {'error': '浏览器已关闭'})
        return
    path = urlsplit(suffix).path
    websocket = handler.headers.get('Upgrade', '').lower() == 'websocket'
    if websocket:
        if path != '/websockify' or handler.headers.get('Origin') != 'http://' + handler.headers.get('Host', ''):
            handler.respond(403, {'error': '浏览器连接来源无效'})
            return
        _websocket(handler, session, suffix)
        return
    if path not in {'/', '/index.html', '/package.json'} and not path.startswith('/assets/'):
        handler.respond(404, {'error': '页面不存在'})
        return
    upstream = http.client.HTTPConnection('127.0.0.1', session.desktop.port, timeout=15)
    try:
        upstream.request('GET', suffix, headers={'Authorization': session.desktop.authorization})
        response = upstream.getresponse()
        data = response.read()
        handler.send_response(response.status)
        handler.send_header('Content-Type', response.getheader('Content-Type', 'application/octet-stream'))
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        # Kasm's bundled client needs inline bootstrapping and blob workers.
        # Keep that policy scoped to its authenticated session, not the console.
        handler.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; worker-src 'self' blob:; connect-src 'self' data: ws: wss:; frame-ancestors 'self'; base-uri 'self'")
        handler.end_headers()
        handler.wfile.write(data)
    finally:
        upstream.close()


def _websocket(handler, session, suffix):
    with socket.create_connection(('127.0.0.1', session.desktop.port), timeout=10) as upstream:
        headers = [f'GET {suffix} HTTP/1.1', f'Host: 127.0.0.1:{session.desktop.port}',
                   'Upgrade: websocket', 'Connection: Upgrade',
                   f'Origin: http://127.0.0.1:{session.desktop.port}',
                   'Authorization: ' + session.desktop.authorization]
        for name in ('Sec-WebSocket-Key', 'Sec-WebSocket-Version', 'Sec-WebSocket-Protocol'):
            if handler.headers.get(name):
                headers.append(name + ': ' + handler.headers[name])
        upstream.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('ascii'))
        response = bytearray()
        while b'\r\n\r\n' not in response:
            part = upstream.recv(4096)
            if not part or len(response) > 65536:
                handler.respond(502, {'error': '浏览器连接未建立'})
                return
            response.extend(part)
        head, remainder = bytes(response).split(b'\r\n\r\n', 1)
        if head.split(b'\r\n', 1)[0].split()[1] != b'101':
            handler.respond(502, {'error': '浏览器连接未建立'})
            return
        handler.close_connection = True
        handler.wfile.write(head + b'\r\n\r\n' + remainder)
        handler.wfile.flush()
        handler.connection.settimeout(10)
        try:
            while session.active and not session.stopped.is_set():
                readable, _, _ = select.select([handler.connection, upstream], [], [], 1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is handler.connection else handler.connection
                    destination.sendall(data)
                    # Only user input extends the lease; animations must not keep an
                    # abandoned browser alive. The wrapper also sends a heartbeat.
                    if source is handler.connection:
                        session.last_activity = time.monotonic()
        except (OSError, ValueError):
            return
