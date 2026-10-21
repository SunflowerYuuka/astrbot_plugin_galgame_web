import asyncio
import importlib
import json
from pathlib import Path

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
session_module = importlib.import_module(f"{PACKAGE_NAME}.api.session")
favorites_module = importlib.import_module(f"{PACKAGE_NAME}.api.favorites")
prefs_module = importlib.import_module(f"{PACKAGE_NAME}.api.prefs")
config_module = importlib.import_module(f"{PACKAGE_NAME}.api.config")
users_module = importlib.import_module(f"{PACKAGE_NAME}.api.users")
identity_module = importlib.import_module(f"{PACKAGE_NAME}.api.identity")
shared_identity = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.identity")
session_helpers = importlib.import_module(
    f"{PACKAGE_NAME}.galgame_web.session_helpers"
)
user_store_module = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.users")


class _Request:
    def __init__(self, data=None, headers=None, args=None):
        self.data = data
        self.headers = headers or {}
        self.args = args or {}

    async def get_json(self):
        return self.data


class _ConversationManager:
    async def delete_conversations_by_user_id(self, _umo):
        return None

    async def get_curr_conversation_id(self, _umo):
        return None


class _Context:
    conversation_manager = _ConversationManager()

    def get_config(self):
        return {"provider_settings": {}, "wake_prefix": ["/"]}


class _Plugin(
    session_module.SessionAPI,
    favorites_module.FavoritesAPI,
    prefs_module.PrefsAPI,
    config_module.ConfigAPI,
    users_module.UserAPI,
    identity_module.IdentityAPI,
):
    def __init__(self, store):
        self._sessions = {}
        self._webchat_username = "tester"
        self._user_store = store
        self._identity_secret = b"secret"
        self.config = {"rapid_click_enabled": True, "rapid_click_threshold": 5}
        self.context = _Context()
        self._tasks = []

    def _track_task(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        self._tasks.append(task)
        return task

    def _get_history_limit(self):
        return 50


def _headers(username):
    timestamp, signature = shared_identity.sign_identity(b"secret", username)
    return {
        shared_identity.IDENTITY_USER_HEADER: username,
        shared_identity.IDENTITY_TS_HEADER: timestamp,
        shared_identity.IDENTITY_SIG_HEADER: signature,
    }


def _as_user(monkeypatch, username, data=None, args=None):
    req = _Request(data, _headers(username), args)
    for module in (
        session_module,
        favorites_module,
        prefs_module,
        users_module,
        identity_module,
    ):
        monkeypatch.setattr(module, "request", req)
    return req


def _write_session(path, session_id, history, owner=""):
    payload = {
        "umo": "umo",
        "conv_id": "conv",
        "owner": owner,
        "history": history,
        "current_emotion": "neutral",
        "created_at": 1,
    }
    (path / f"{session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _build(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    audio_dir = tmp_path / "audio"
    assets_dir = tmp_path / "assets"
    for directory in (sessions_dir, audio_dir, assets_dir):
        directory.mkdir()
    monkeypatch.setattr(session_helpers, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(session_helpers, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(session_helpers, "ASSETS_DIR", assets_dir)
    monkeypatch.setattr(
        session_helpers, "FAVORITES_PATH", tmp_path / "favorites.json"
    )
    monkeypatch.setattr(session_helpers, "PREFS_PATH", tmp_path / "prefs.json")
    monkeypatch.setattr(
        session_helpers, "PREFS_USERS_PATH", tmp_path / "prefs_users.json"
    )

    store = user_store_module.UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-pass")
    store.add("alice", "alice-pass", is_admin=False)
    store.add("bob", "bob-pass", is_admin=False)
    plugin = _Plugin(store)
    return plugin, store, sessions_dir, audio_dir


def _create_session(monkeypatch, plugin, username):
    _as_user(monkeypatch, username, {"force_new": True})
    result = asyncio.run(plugin._api_session_init())
    assert "session_id" in result, result
    return result["session_id"]


def test_sessions_are_listed_and_scoped_per_user(tmp_path, monkeypatch):
    plugin, _, sessions_dir, _ = _build(tmp_path, monkeypatch)
    alice_sid = _create_session(monkeypatch, plugin, "alice")
    bob_sid = _create_session(monkeypatch, plugin, "bob")
    assert alice_sid != bob_sid
    assert plugin._sessions[alice_sid]["owner"] == "alice"
    assert plugin._sessions[bob_sid]["owner"] == "bob"

    _as_user(monkeypatch, "alice")
    alice_list = asyncio.run(plugin._api_session_list())
    assert [item["session_id"] for item in alice_list["sessions"]] == [
        alice_sid
    ]

    _as_user(monkeypatch, "bob")
    bob_list = asyncio.run(plugin._api_session_list())
    assert [item["session_id"] for item in bob_list["sessions"]] == [bob_sid]

    _as_user(monkeypatch, "bob", args={"session_id": alice_sid})
    assert asyncio.run(plugin._api_history()) == (
        {"error": "session not found"},
        404,
    )

    _as_user(monkeypatch, "bob", {"session_id": alice_sid})
    assert asyncio.run(plugin._api_session_delete()) == (
        {"error": "session not found"},
        404,
    )
    assert (sessions_dir / f"{alice_sid}.json").exists()

    _as_user(
        monkeypatch,
        "bob",
        {"session_id": alice_sid, "text": "hello", "audio_data": ""},
    )
    assert asyncio.run(plugin._api_send()) == (
        {"error": "session not found"},
        404,
    )

    _as_user(monkeypatch, "bob", {"session_id": alice_sid, "count": 10})
    assert asyncio.run(plugin._api_rapid_action()) == (
        {"error": "session not found"},
        404,
    )

    _as_user(monkeypatch, "bob", {"resume_id": alice_sid})
    resumed_other = asyncio.run(plugin._api_session_init())
    assert resumed_other["session_id"] != alice_sid
    assert plugin._sessions[resumed_other["session_id"]]["owner"] == "bob"

    _as_user(monkeypatch, "alice", {"resume_id": alice_sid})
    resumed = asyncio.run(plugin._api_session_init())
    assert resumed["session_id"] == alice_sid


def test_legacy_sessions_belong_to_admin(tmp_path, monkeypatch):
    plugin, _, sessions_dir, _ = _build(tmp_path, monkeypatch)
    sid = "a" * 32
    _write_session(
        sessions_dir,
        sid,
        [{"role": "assistant", "content": "legacy"}],
        owner="",
    )
    plugin._sessions[sid] = session_helpers.load_session(sid)

    _as_user(monkeypatch, "alice")
    alice_list = asyncio.run(plugin._api_session_list())
    assert alice_list["sessions"] == []
    _as_user(monkeypatch, "alice", {"resume_id": sid})
    created = asyncio.run(plugin._api_session_init())
    assert created["session_id"] != sid
    assert plugin._sessions[created["session_id"]]["owner"] == "alice"

    _as_user(monkeypatch, "admin")
    admin_list = asyncio.run(plugin._api_session_list())
    assert [item["session_id"] for item in admin_list["sessions"]] == [sid]


def test_favorites_and_prefs_are_scoped_per_user(tmp_path, monkeypatch):
    plugin, _, _, audio_dir = _build(tmp_path, monkeypatch)
    alice_sid = _create_session(monkeypatch, plugin, "alice")
    bob_sid = _create_session(monkeypatch, plugin, "bob")

    (audio_dir / "alice.wav").write_bytes(b"RIFF")
    (audio_dir / "bob.wav").write_bytes(b"RIFF")
    for sid, audio in ((alice_sid, "alice.wav"), (bob_sid, "bob.wav")):
        plugin._sessions[sid]["history"].append(
            {"role": "assistant", "content": "hi", "audio_file": audio}
        )
        session_helpers.save_session(plugin._sessions, sid)

    _as_user(
        monkeypatch,
        "alice",
        {"text": "alice voice", "audio_file": "alice.wav", "audio_mime": "audio/wav"},
    )
    assert asyncio.run(plugin._api_favorites_add()) == {"status": "ok"}
    _as_user(monkeypatch, "bob")
    assert asyncio.run(plugin._api_favorites_list()) == {"favorites": []}
    _as_user(
        monkeypatch,
        "bob",
        {"text": "steal", "audio_file": "alice.wav", "audio_mime": "audio/wav"},
    )
    assert asyncio.run(plugin._api_favorites_add()) == (
        {"error": "audio file not found"},
        404,
    )

    _as_user(monkeypatch, "alice")
    alice_favs = asyncio.run(plugin._api_favorites_list())["favorites"]
    assert len(alice_favs) == 1
    _as_user(monkeypatch, "bob", {"id": alice_favs[0]["id"]})
    assert asyncio.run(plugin._api_favorites_delete()) == (
        {"error": "not found"},
        404,
    )

    _as_user(monkeypatch, "alice", {"bgm_volume": 0.3})
    assert asyncio.run(plugin._api_prefs_set()) == {"status": "ok"}
    _as_user(monkeypatch, "bob", {"bgm_volume": 0.7})
    assert asyncio.run(plugin._api_prefs_set()) == {"status": "ok"}
    _as_user(monkeypatch, "alice")
    assert asyncio.run(plugin._api_prefs_get())["bgm_volume"] == 0.3
    _as_user(monkeypatch, "bob")
    assert asyncio.run(plugin._api_prefs_get())["bgm_volume"] == 0.7


def test_admin_can_manage_users_and_delete_their_data(tmp_path, monkeypatch):
    plugin, store, sessions_dir, audio_dir = _build(tmp_path, monkeypatch)
    carol_sid = _create_session(monkeypatch, plugin, "admin")
    plugin._sessions[carol_sid]["owner"] = "carol"

    _as_user(monkeypatch, "alice")
    assert asyncio.run(plugin._api_users_list()) == (
        {"error": "admin permission required"},
        403,
    )

    _as_user(monkeypatch, "admin", {"username": "carol", "password": "carol-pass"})
    assert asyncio.run(plugin._api_users_add())["status"] == "ok"
    assert store.verify("carol", "carol-pass")

    _as_user(monkeypatch, "admin")
    listed = asyncio.run(plugin._api_users_list())["users"]
    assert {"admin", "alice", "bob", "carol"} == {
        user["username"] for user in listed
    }

    _as_user(monkeypatch, "carol")
    assert asyncio.run(plugin._api_session_list())["sessions"] == []
    plugin._sessions[carol_sid]["owner"] = "carol"
    session_helpers.save_session(plugin._sessions, carol_sid)
    assert asyncio.run(plugin._api_session_list())["sessions"][0][
        "session_id"
    ] == carol_sid

    _as_user(monkeypatch, "admin", {"username": "carol"})
    result = asyncio.run(plugin._api_users_delete())
    assert result["status"] == "ok"
    assert not store.has_user("carol")
    assert not (sessions_dir / f"{carol_sid}.json").exists()
    assert carol_sid not in plugin._sessions

    _as_user(monkeypatch, "admin", {"username": "admin"})
    assert asyncio.run(plugin._api_users_delete()) == (
        {"error": "cannot delete the last admin"},
        400,
    )


def test_legacy_sessions_and_prefs_migrate_to_admin(tmp_path, monkeypatch):
    plugin, _, sessions_dir, _ = _build(tmp_path, monkeypatch)
    sid = "b" * 32
    _write_session(
        sessions_dir,
        sid,
        [{"role": "assistant", "content": "legacy"}],
        owner="",
    )
    sessions = {}
    assert session_helpers.load_all_sessions(sessions, legacy_owner="admin") == 1
    assert sessions[sid]["owner"] == "admin"
    stored = json.loads((sessions_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert stored["owner"] == "admin"

    session_helpers.save_prefs({"font_style": "serif"})
    assert session_helpers.load_prefs("admin") == {"font_style": "serif"}
    assert session_helpers.load_prefs("alice") == {}
    session_helpers.save_prefs("alice", {"font_style": "sans"})
    assert session_helpers.load_prefs("alice") == {"font_style": "sans"}
    assert plugin is not None


def test_bgm_delete_clears_all_user_preferences(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch)
    session_helpers.save_prefs({"bgm_file": "song.mp3"})
    session_helpers.save_prefs("alice", {"bgm_file": "song.mp3"})
    session_helpers.save_prefs("bob", {"bgm_file": "song.mp3"})
    session_helpers.save_prefs("carol", {"bgm_file": "other.mp3"})

    assert session_helpers.clear_bgm_file_pref("song.mp3") == 3
    assert session_helpers.load_prefs("alice")["bgm_file"] == ""
    assert session_helpers.load_prefs("bob")["bgm_file"] == ""
    assert session_helpers.load_prefs("carol")["bgm_file"] == "other.mp3"
    assert session_helpers.load_prefs()["bgm_file"] == ""


def test_config_reports_current_account_and_user_prefs(tmp_path, monkeypatch):
    plugin, _, _, _ = _build(tmp_path, monkeypatch)
    _as_user(monkeypatch, "alice", {"bgm_volume": 0.25})
    assert asyncio.run(plugin._api_prefs_set()) == {"status": "ok"}
    _as_user(monkeypatch, "alice")
    config = asyncio.run(plugin._api_config())
    assert config["username"] == "alice"
    assert config["is_admin"] is False
    assert config["bgm_volume"] == 0.25
