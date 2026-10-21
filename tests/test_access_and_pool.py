import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE = Path(__file__).resolve().parents[1].name


def test_policy_blacklist_wins_and_admin_can_pass_whitelist():
    policy = importlib.import_module(f"{PACKAGE}.galgame_web.access")
    config = {"web_access_mode": "whitelist", "web_whitelist": ["alice"],
              "web_blacklist": ["bob"], "web_admins": ["bob", "carol"]}
    assert policy.can_use(config, "web", "alice")
    assert not policy.can_use(config, "web", "bob", admin=True)
    assert policy.can_use(config, "web", "carol", admin=True)
    assert not policy.can_use(config, "web", "dave")


def test_platform_qualified_ids_do_not_block_other_platform():
    policy = importlib.import_module(f"{PACKAGE}.galgame_web.access")
    config = {"platform_blacklist": ["qq:123"]}
    assert not policy.can_use(config, "platform", "123", platform="qq")
    assert policy.can_use(config, "platform", "123", platform="telegram")


def test_valid_existing_cookie_stops_working_when_blacklisted(tmp_path, monkeypatch):
    import time
    web = importlib.import_module(f"{PACKAGE}.galgame_web.web_handler")
    users = importlib.import_module(f"{PACKAGE}.galgame_web.users")
    store = users.UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-password")
    store.add("alice", "alice-password")
    monkeypatch.setattr(web.GalgameWebHandler, "user_store", store)
    monkeypatch.setattr(web.GalgameWebHandler, "auth_secret", b"secret")
    config = {}
    monkeypatch.setattr(web.GalgameWebHandler, "access_config", config)
    handler = object.__new__(web.GalgameWebHandler)
    handler.headers = {"Cookie": "galgame_auth=" + web._make_user_auth_token(b"secret", "alice", int(time.time()))}
    assert handler._auth_ok() == "alice"
    config["web_blacklist"] = ["alice"]
    assert handler._auth_ok() is None


def test_configured_web_admin_can_manage_accounts(tmp_path):
    users = importlib.import_module(f"{PACKAGE}.galgame_web.users")
    api_module = importlib.import_module(f"{PACKAGE}.api.users")
    store = users.UserStore(tmp_path / "users.json")
    store.ensure_bootstrap("admin-password")
    store.add("moderator", "mod-password")
    api = api_module.UserAPI()
    api._user_store = store
    api.config = {"web_admins": ["moderator"]}
    api._current_user = lambda: "moderator"
    assert asyncio.run(api._api_me())["is_admin"] is True
    assert len(asyncio.run(api._api_users_list())["users"]) == 2


def test_web_command_returns_configured_url_and_blocks_sender():
    main = importlib.import_module(f"{PACKAGE}.main")
    plugin = object.__new__(main.GalgamePlugin)
    plugin.config = {"web_public_url": "https://galgame.example.com",
                     "platform_blacklist": ["123"]}

    class Event:
        def get_sender_id(self):
            return "123"

        def get_platform_name(self):
            return "aiocqhttp"

        def plain_result(self, text):
            return text

    async def collect():
        return [item async for item in plugin.cmd_galgame(Event(), "web")]

    assert "无权" in asyncio.run(collect())[0]
    plugin.config["platform_blacklist"] = []
    assert "https://galgame.example.com" in asyncio.run(collect())[0]
    for invalid in ("", "ftp://example.com", "https://[invalid"):
        plugin.config["web_public_url"] = invalid
        assert "web_public_url" in asyncio.run(collect())[0]


def test_web_command_custom_multiline_reply_keeps_access_control():
    main = importlib.import_module(f"{PACKAGE}.main")
    plugin = object.__new__(main.GalgamePlugin)
    text = "网页入口：https://galgame.example.com\n使用 Cloudflare 转发，国内网络可能访问不稳定。\n打不开可稍后重试。"
    plugin.config = {"web_command_reply": text, "web_public_url": "not-a-url"}

    class Event:
        def get_sender_id(self):
            return "123"

        def get_platform_name(self):
            return "aiocqhttp"

        def plain_result(self, value):
            return value

    async def collect():
        return [item async for item in plugin.cmd_galgame(Event(), "web")]

    assert asyncio.run(collect()) == [text]
    plugin.config["web_command_reply"] = "仅文字说明，不包含网址也可以。"
    assert asyncio.run(collect()) == [plugin.config["web_command_reply"]]
    plugin.config["platform_blacklist"] = ["123"]
    assert "无权" in asyncio.run(collect())[0]
    plugin.config["platform_blacklist"] = []
    plugin.config["web_command_reply"] = " \n "
    plugin.config["web_public_url"] = "https://galgame.example.com"
    assert asyncio.run(collect()) == ["AI Galgame 网页入口：\nhttps://galgame.example.com"]


def test_pool_snapshot_requires_review_and_never_exposes_private_fields(tmp_path):
    module = importlib.import_module(f"{PACKAGE}.galgame_web.public_pool")
    store = module.PoolStore(tmp_path / "pool.json")
    source = {"history": [{"role": "user", "content": "hello", "audio_file": "secret.wav"},
                          {"role": "assistant", "content": "hi", "id": "private"}],
              "conv_id": "secret", "umo": "secret"}
    item = store.submit("alice", "My story", "description", source)
    assert store.list_visible("bob") == []
    assert store.get_visible(item["id"], "bob") is None
    assert len(store.list_visible("alice")) == 1
    assert store.review(item["id"], "moderator", "approved", "ok")
    visible = store.get_visible(item["id"], "bob")
    assert visible["history"] == [{"role": "user", "content": "hello"},
                                  {"role": "assistant", "content": "hi"}]
    source["history"][0]["content"] = "changed"
    assert visible["history"][0]["content"] == "hello"
    assert "history" not in store.list_visible("bob")[0]
    assert not store.review(item["id"], "moderator", "approved", "again")
    with pytest.raises(ValueError):
        store.submit("alice", "empty", "", {"history": []})


def test_rejected_submission_only_visible_to_owner_and_admin(tmp_path):
    module = importlib.import_module(f"{PACKAGE}.galgame_web.public_pool")
    path = tmp_path / "pool.json"
    store = module.PoolStore(path)
    item = store.submit("alice", "story", "", {"history": [{"role": "user", "content": "hi"}]})
    assert store.review(item["id"], "admin", "rejected", "needs editing")
    store = module.PoolStore(path)
    assert store.get_visible(item["id"], "bob") is None
    assert store.get_visible(item["id"], "alice")["reason"] == "needs editing"
    assert len(store.list_visible("admin", admin=True)) == 1
    assert not store.remove(item["id"], "bob")
    assert store.remove(item["id"], "alice")
    assert store.list_visible("admin", admin=True) == []


def test_pool_api_denies_other_session_and_claims_independent_copy(tmp_path, monkeypatch):
    api_module = importlib.import_module(f"{PACKAGE}.api.pool")
    session = importlib.import_module(f"{PACKAGE}.galgame_web.session_helpers")
    pool = importlib.import_module(f"{PACKAGE}.galgame_web.public_pool")
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path / "sessions")
    session.SESSIONS_DIR.mkdir()

    class API(api_module.PoolAPI):
        def _current_user(self):
            return self.user

        def _is_admin(self, user):
            return user == "admin"

    api = API()
    api.user = "bob"
    api._pool_store = pool.PoolStore(tmp_path / "pool.json")
    sid = "a" * 32
    api._sessions = {sid: {"owner": "alice", "history": [{"role": "user", "content": "hi"}]}}
    api._webchat_username = "astrbot"
    api.config = {}

    class Conversations:
        async def get_curr_conversation_id(self, umo):
            return None

        async def new_conversation(self, **kwargs):
            self.content = kwargs["content"]
            return "new-conv"

    conversations = Conversations()
    api.context = SimpleNamespace(conversation_manager=conversations)

    class Request:
        def __init__(self):
            self.args = {}
            self.data = {"session_id": sid, "title": "story", "description": ""}

        async def get_json(self):
            return self.data

    req = Request()
    monkeypatch.setattr(api_module, "request", req)
    assert asyncio.run(api._api_pool_submit())[1] == 404
    api.user = "alice"
    result = asyncio.run(api._api_pool_submit())
    pool_id = result["id"]
    req.data = {"id": pool_id}
    api.user = "bob"
    assert asyncio.run(api._api_pool_claim())[1] == 404
    req.data = {"id": pool_id, "status": "approved", "reason": "ok"}
    assert asyncio.run(api._api_pool_review())[1] == 403
    api.user = "admin"
    assert asyncio.run(api._api_pool_review())["status"] == "ok"
    req.data = {"id": pool_id}
    api.user = "bob"
    claimed = asyncio.run(api._api_pool_claim())
    fresh = claimed["session_id"]
    assert fresh != sid
    assert api._sessions[fresh]["owner"] == "bob"
    assert api._sessions[fresh]["conv_id"] == "new-conv"
    assert conversations.content[0]["content"] == "hi"
    assert fresh in api._sessions[fresh]["umo"]
    api._sessions[fresh]["history"][0]["content"] = "edited"
    assert api._pool_store.get_visible(pool_id, "bob")["history"][0]["content"] == "hi"
    second = asyncio.run(api._api_pool_claim())
    assert second["session_id"] != fresh
    assert api._sessions[second["session_id"]]["history"][0]["content"] == "hi"

    async def fail(**kwargs):
        raise RuntimeError("database unavailable")

    conversations.new_conversation = fail
    before = set(api._sessions)
    assert asyncio.run(api._api_pool_claim())[1] == 502
    assert set(api._sessions) == before
    assert len(list(session.SESSIONS_DIR.glob("*.json"))) == 2
