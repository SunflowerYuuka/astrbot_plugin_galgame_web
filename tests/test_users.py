import importlib
import json
from pathlib import Path

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
users_module = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.users")
UserStore = users_module.UserStore


def test_bootstrap_creates_admin_with_hashed_password(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path)
    assert store.ensure_bootstrap("correct horse") == "admin"
    assert store.has_users()
    assert store.has_user("admin")
    assert store.is_admin("admin")
    assert store.verify("admin", "correct horse")
    assert not store.verify("admin", "wrong")

    raw = path.read_text(encoding="utf-8")
    assert "correct horse" not in raw
    data = json.loads(raw)
    assert data["users"]["admin"]["hash"]
    assert data["users"]["admin"]["salt"]
    assert data["users"]["admin"]["iterations"] >= 100_000


def test_add_list_delete_and_set_password(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-pass")
    store.add("alice", "alice-pass", is_admin=False)
    store.add("bob", "bob-pass", is_admin=True)

    assert store.verify("alice", "alice-pass")
    assert store.verify("bob", "bob-pass")
    assert store.admin_count() == 2
    listed = {user["username"]: user for user in store.list_users()}
    assert listed["alice"]["is_admin"] is False
    assert "hash" not in listed["alice"]

    assert store.set_password("alice", "new-alice-pass")
    assert store.verify("alice", "new-alice-pass")
    assert not store.verify("alice", "alice-pass")

    assert store.delete("alice")
    assert not store.has_user("alice")
    assert not store.delete("alice")


def test_duplicate_and_invalid_credentials_are_rejected(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-pass")
    store.add("alice", "alice-pass")

    for username, password in (
        ("Alice", "another-pass"),
        ("bad name", "another-pass"),
        ("-bad", "another-pass"),
        ("ok", "short"),
        ("ok", "x" * 300),
    ):
        try:
            store.add(username, password)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected rejection for {username!r}")


def test_default_owner_prefers_admin(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.default_owner() == ""
    store.ensure_bootstrap("admin-pass")
    store.add("alice", "alice-pass", is_admin=False)
    assert store.default_owner() == "admin"
