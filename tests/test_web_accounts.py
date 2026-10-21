import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from galgame_web.identity import verify_identity
from galgame_web.users import UserStore
from galgame_web.web_handler import GalgameWebHandler


class _UpstreamHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, fmt, *args):
        pass

    def _respond(self):
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "user": self.headers.get("X-Galgame-User"),
                "ts": self.headers.get("X-Galgame-User-Ts"),
                "sig": self.headers.get("X-Galgame-User-Sig"),
            }
        )
        body = json.dumps({"ok": True, "user": self.headers.get("X-Galgame-User")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self._respond()


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_account_login_cookie_identity_headers_and_logout(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-pass")
    store.add("alice", "alice-pass")

    upstream, upstream_thread = _start(_UpstreamHandler)
    GalgameWebHandler.upstream = f"http://127.0.0.1:{upstream.server_port}"
    GalgameWebHandler.web_password = ""
    GalgameWebHandler.user_store = store
    GalgameWebHandler.auth_configured = True
    GalgameWebHandler.auth_secret = b"secret"
    GalgameWebHandler.jwt_token = ""
    GalgameWebHandler.jwt_token_factory = lambda: "test-token"
    GalgameWebHandler.audio_owner_check = None
    GalgameWebHandler._login_failures.clear()
    _UpstreamHandler.requests = []

    web, web_thread = _start(GalgameWebHandler)
    connection = http.client.HTTPConnection(
        "127.0.0.1", web.server_port, timeout=5
    )
    origin = f"http://127.0.0.1:{web.server_port}"
    try:
        connection.request("GET", "/login")
        response = connection.getresponse()
        response.read()
        assert response.status == 200
        assert response.getheader("Referrer-Policy") == "same-origin"

        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        assert response.status == 302
        assert response.getheader("Location") == "/login"

        payload = "username=alice&password=alice-pass"
        connection.request(
            "POST",
            "/api/login",
            body=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(payload)),
                "Origin": origin,
            },
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 302
        cookie_header = response.getheader("Set-Cookie")
        assert "HttpOnly" in cookie_header
        assert "SameSite=Strict" in cookie_header
        cookie = cookie_header.split(";", 1)[0]

        connection.request("GET", "/", headers={"Cookie": cookie})
        response = connection.getresponse()
        response.read()
        assert response.status == 200

        connection.request(
            "GET",
            "/api/plug/astrbot_plugin_galgame_web/config",
            headers={"Cookie": cookie},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert body == {"ok": True, "user": "alice"}
        forwarded = _UpstreamHandler.requests[-1]
        assert forwarded["authorization"] == "Bearer test-token"
        assert forwarded["user"] == "alice"
        assert verify_identity(
            b"secret", "alice", forwarded["ts"], forwarded["sig"]
        )

        connection.request(
            "GET", "/api/plug/astrbot_plugin_galgame_web/config"
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 401

        connection.request(
            "POST", "/api/logout", body="", headers={"Cookie": cookie}
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 200
        assert "Max-Age=0" in response.getheader("Set-Cookie")

        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        assert response.status == 302
    finally:
        connection.close()
        _stop(web, web_thread)
        _stop(upstream, upstream_thread)
        GalgameWebHandler.user_store = None
        GalgameWebHandler.auth_configured = False
        GalgameWebHandler.web_password = ""
        GalgameWebHandler.jwt_token_factory = None
        GalgameWebHandler._login_failures.clear()


def test_wrong_account_password_is_rejected(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-pass")
    GalgameWebHandler.web_password = ""
    GalgameWebHandler.user_store = store
    GalgameWebHandler.auth_configured = True
    GalgameWebHandler.auth_secret = b"secret"
    GalgameWebHandler._login_failures.clear()

    web, web_thread = _start(GalgameWebHandler)
    connection = http.client.HTTPConnection(
        "127.0.0.1", web.server_port, timeout=5
    )
    origin = f"http://127.0.0.1:{web.server_port}"
    try:
        payload = "username=admin&password=wrong"
        connection.request(
            "POST",
            "/api/login",
            body=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(payload)),
                "Origin": origin,
            },
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 302
        assert response.getheader("Location") == "/login?err=1"
        assert response.getheader("Set-Cookie") is None
    finally:
        connection.close()
        _stop(web, web_thread)
        GalgameWebHandler.user_store = None
        GalgameWebHandler.auth_configured = False
        GalgameWebHandler.web_password = ""
        GalgameWebHandler._login_failures.clear()
