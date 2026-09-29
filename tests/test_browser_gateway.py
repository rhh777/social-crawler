import base64
import hashlib
import json
import socket
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from social_crawler.interfaces.web import Handler


@pytest.fixture
def gateway():
    observed = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            observed.append((self.path, self.headers.get('Authorization')))
            if self.headers.get('Upgrade') == 'websocket':
                assert self.headers['Origin'] == f'http://127.0.0.1:{self.server.server_port}'
                accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n'.encode())
                self.wfile.flush()
                payload = self.rfile.read(8)
                self.wfile.write(payload)
                self.wfile.flush()
            else:
                payload = b'<html>KasmVNC assets</html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    session = SimpleNamespace(id='session-a', active=True, state='open', stopped=threading.Event(),
                              access_token='session-secret', last_activity=0,
                              desktop=SimpleNamespace(port=upstream.server_port, authorization='Basic upstream-secret'))
    app = SimpleNamespace(account_browsers={'account-a':session}, token='console-token',
                          browser_session=lambda session_id: session if session_id == session.id else None,
                          open_account_browser=lambda _, **kwargs: {
                              'id':session.id, 'state':session.state
                          })
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = app
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in [upstream, server]]
    for thread in threads:
        thread.start()
    yield server, session, observed
    session.stopped.set()
    for item in [server, upstream]:
        item.shutdown()
        item.server_close()
    for thread in threads:
        thread.join(timeout=5)


def request(server, method='GET', path='/browser/session-a/', headers=None, body=None):
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_session_cookie_is_required_scoped_and_not_public(gateway):
    server, session, observed = gateway
    assert request(server)[0] == 403
    assert request(server, headers={'Cookie':'crawler_browser=another-session'})[0] == 403
    assert not observed
    status, headers, body = request(server, 'POST', '/api/accounts/account-a/browser',
                                     {'X-Console-Token':'console-token'}, json.dumps({'kind':'open'}))
    assert status == 200
    assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    assert 'Path=/browser/session-a/' in headers['Set-Cookie']
    assert session.access_token.encode() not in body
    status, headers, body = request(server, headers={'Cookie':'crawler_browser=session-secret'})
    assert status == 200 and b'KasmVNC' in body
    assert observed == [('/', 'Basic upstream-secret')]
    assert b'upstream-secret' not in body
    assert "frame-ancestors 'self'" in headers['Content-Security-Policy']
    assert 'connect-src' in headers['Content-Security-Policy'] and 'data:' in headers['Content-Security-Policy']
    session.active = False
    assert request(server, headers={'Cookie':'crawler_browser=session-secret'})[0] == 410


def test_websocket_rejects_foreign_origin_and_forwards_binary(gateway):
    server, session, observed = gateway
    headers = {'Cookie':'crawler_browser=session-secret','Upgrade':'websocket','Origin':'http://foreign.invalid'}
    assert request(server, path='/browser/session-a/websockify', headers=headers)[0] == 403
    assert not observed
    with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as client:
        client.sendall((f'GET /browser/session-a/websockify HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nOrigin: http://127.0.0.1:{server.server_port}\r\nCookie: crawler_browser=session-secret\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n').encode())
        response = b''
        while not response.endswith(b'\r\n\r\n'):
            response += client.recv(1)
        assert b'101 Switching Protocols' in response
        assert b's3pPLMBiTxaQ9kYGzzhZRbK+xOo=' in response
        payload = b'\x82\x82\x01\x02\x03\x04\x44\x55'
        client.sendall(payload)
        received = b''
        while len(received) < len(payload):
            received += client.recv(len(payload) - len(received))
        assert received == payload
    assert session.last_activity > 0
    assert observed == [('/websockify', 'Basic upstream-secret')]


def test_gateway_does_not_expose_kasm_management_api(gateway):
    server, _, observed = gateway
    assert request(server, path='/browser/session-a/api/create_user', headers={'Cookie':'crawler_browser=session-secret'})[0] == 404
    assert not observed
