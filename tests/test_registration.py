import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from galgame_web.users import UserStore
from galgame_web.web_handler import GalgameWebHandler, MAX_LOGIN_FAILURES


@pytest.fixture
def registration_server(tmp_path, monkeypatch):
    store = UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-password")
    config = {"web_registration_enabled": True}
    monkeypatch.setattr(GalgameWebHandler, "user_store", store)
    monkeypatch.setattr(GalgameWebHandler, "access_config", config)
    monkeypatch.setattr(GalgameWebHandler, "auth_configured", True)
    monkeypatch.setattr(GalgameWebHandler, "web_password", "")
    monkeypatch.setattr(GalgameWebHandler, "auth_secret", b"registration-secret")
    monkeypatch.setattr(GalgameWebHandler, "_login_failures", {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), GalgameWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def send(path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        request_headers = {"Origin": origin, **(headers or {})}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(data)
        else:
            body = None
        try:
            connection.request("POST" if data is not None else "GET", path, body, request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    yield store, config, send
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def signup(send, username="alice", **extra):
    return send("/api/register", {
        "username": username, "password": "alice-password",
        "confirm_password": "alice-password", **extra,
    })


def test_register_creates_normal_account_and_logs_in_immediately(registration_server):
    store, _, send = registration_server
    assert 'href="/register"' in send("/login")[2]
    status, _, page = send("/register")
    assert status == 200
    assert 'name="confirm_password"' in page
    status, headers, body = signup(send, "Alice", is_admin=True)
    assert status == 201
    assert json.loads(body)["username"] == "alice"
    assert store.verify("alice", "alice-password")
    assert not store.is_admin("alice")
    assert store.admin_count() == 1
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, _, _ = send("/", headers={"Cookie": cookie.split(";", 1)[0]})
    assert status == 200


def test_registration_disabled_blocks_page_api_and_hides_link(registration_server):
    store, config, send = registration_server
    config["web_registration_enabled"] = False
    assert 'href="/register"' not in send("/login")[2]
    assert send("/register")[0] == 403
    assert signup(send)[0] == 403
    assert not store.has_user("alice")


def test_registration_defaults_to_enabled_and_uses_secure_cookie(registration_server):
    store, config, send = registration_server
    config.clear()
    assert 'href="/register"' in send("/login")[2]
    status, headers, _ = send("/api/register", {
        "username": "alice", "password": "alice-password", "confirm_password": "alice-password",
    }, headers={"X-Forwarded-Proto": "https"})
    assert status == 201
    assert "Secure" in headers["Set-Cookie"]
    assert not store.is_admin("alice")


def test_registration_rejects_non_object_json(registration_server):
    store, _, send = registration_server
    assert send("/api/register", ["alice", "alice-password"])[0] == 400
    assert not store.has_user("alice")


@pytest.mark.parametrize("changes", [
    {"password": "short", "confirm_password": "short"},
    {"confirm_password": "different"},
    {"username": "bad name"},
    {"username": ""},
    {"username": []},
])
def test_invalid_registration_does_not_create_user(registration_server, changes):
    store, _, send = registration_server
    payload = {"username": "alice", "password": "alice-password", "confirm_password": "alice-password"}
    payload.update(changes)
    status, headers, _ = send("/api/register", payload)
    assert status == 400
    assert "Set-Cookie" not in headers
    assert [user["username"] for user in store.list_users()] == ["admin"]


def test_duplicate_registration_preserves_original_password(registration_server):
    store, _, send = registration_server
    assert signup(send)[0] == 201
    assert signup(send, "ALICE", password="new-password", confirm_password="new-password")[0] == 409
    assert store.verify("alice", "alice-password")
    assert not store.verify("alice", "new-password")


def test_registration_respects_blacklist_and_whitelist(registration_server):
    store, config, send = registration_server
    config["web_blacklist"] = ["alice"]
    assert signup(send)[0] == 403
    assert not store.has_user("alice")
    config["web_blacklist"] = []
    config["web_access_mode"] = "whitelist"
    config["web_whitelist"] = ["bob"]
    assert signup(send)[0] == 403
    assert signup(send, "bob")[0] == 201


def test_registration_cannot_claim_configured_admin_name(registration_server):
    store, config, send = registration_server
    config["web_admins"] = ["moderator"]
    assert signup(send, "moderator")[0] == 403
    assert not store.has_user("moderator")


def test_registration_requires_initial_admin_setup(registration_server):
    store, _, send = registration_server
    store.delete("admin")
    assert signup(send)[0] == 503
    assert not store.has_users()


def test_cross_origin_registration_is_rejected(registration_server):
    store, _, send = registration_server
    status, _, _ = send("/api/register", {
        "username": "alice", "password": "alice-password", "confirm_password": "alice-password",
    }, headers={"Origin": "https://other.example"})
    assert status == 403
    assert not store.has_user("alice")


def test_registration_rate_limit_is_shared_across_usernames(registration_server):
    store, _, send = registration_server
    for index in range(MAX_LOGIN_FAILURES):
        assert signup(send, f"user{index}")[0] == 201
    assert signup(send, "another")[0] == 429
    assert not store.has_user("another")
