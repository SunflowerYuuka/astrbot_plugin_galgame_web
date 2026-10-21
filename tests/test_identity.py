import importlib
from pathlib import Path

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
identity_module = importlib.import_module(f"{PACKAGE_NAME}.api.identity")
shared_identity = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.identity")
users_module = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.users")


class _Request:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _headers(username, secret=b"secret", ts=None):
    timestamp, signature = shared_identity.sign_identity(secret, username, ts)
    return {
        shared_identity.IDENTITY_USER_HEADER: username,
        shared_identity.IDENTITY_TS_HEADER: timestamp,
        shared_identity.IDENTITY_SIG_HEADER: signature,
    }


class _Plugin(identity_module.IdentityAPI):
    def __init__(self, store):
        self._identity_secret = b"secret"
        self._user_store = store


def test_sign_and_verify_identity(monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(shared_identity.time, "time", lambda: now)
    ts, sig = shared_identity.sign_identity(b"secret", "alice", now)
    assert shared_identity.verify_identity(b"secret", "alice", ts, sig)
    assert not shared_identity.verify_identity(b"wrong", "alice", ts, sig)
    assert not shared_identity.verify_identity(b"secret", "bob", ts, sig)
    assert not shared_identity.verify_identity(
        b"secret", "alice", now - 121, sig
    )
    assert not shared_identity.verify_identity(b"secret", "alice", ts, "bad")


def test_current_user_from_signed_headers(tmp_path, monkeypatch):
    store = users_module.UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-pass")
    store.add("alice", "alice-pass")
    plugin = _Plugin(store)

    monkeypatch.setattr(identity_module, "request", _Request(_headers("alice")))
    assert plugin._current_user() == "alice"

    monkeypatch.setattr(identity_module, "request", _Request({}))
    assert plugin._current_user() == "admin"

    bad = _headers("alice")
    bad[shared_identity.IDENTITY_SIG_HEADER] = "0" * 64
    monkeypatch.setattr(identity_module, "request", _Request(bad))
    assert plugin._current_user() is None

    store.delete("alice")
    monkeypatch.setattr(identity_module, "request", _Request(_headers("alice")))
    assert plugin._current_user() is None


def test_resolve_owner_falls_back_for_legacy_callers():
    class _Legacy:
        pass

    assert identity_module.resolve_owner(_Legacy()) == "admin"
