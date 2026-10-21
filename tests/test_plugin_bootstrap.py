import asyncio
import importlib
from pathlib import Path

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
main_module = importlib.import_module(f"{PACKAGE_NAME}.main")
session_helpers = importlib.import_module(
    f"{PACKAGE_NAME}.galgame_web.session_helpers"
)
users_module = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.users")
web_handler_module = importlib.import_module(
    f"{PACKAGE_NAME}.galgame_web.web_handler"
)


class _Context:
    def __init__(self):
        self.registered_web_apis = []

    def get_config(self):
        return {
            "dashboard": {"username": "astrbot", "port": 6185},
            "provider_settings": {},
            "wake_prefix": ["/"],
        }

    def register_web_api(self, *args):
        self.registered_web_apis.append(args)


def _patch_paths(tmp_path, monkeypatch):
    for name, value in {
        "SESSIONS_DIR": tmp_path / "sessions",
        "AUDIO_DIR": tmp_path / "audio",
        "ASSETS_DIR": tmp_path / "assets",
        "BGM_DIR": tmp_path / "bgm",
        "FAVORITES_PATH": tmp_path / "favorites.json",
        "PREFS_PATH": tmp_path / "prefs.json",
        "PREFS_USERS_PATH": tmp_path / "prefs_users.json",
    }.items():
        monkeypatch.setattr(session_helpers, name, value)
    monkeypatch.setattr(users_module, "USERS_PATH", tmp_path / "users.json")


def test_plugin_bootstraps_admin_from_web_password(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)

    async def scenario():
        plugin = main_module.GalgamePlugin(
            _Context(),
            {
                "web_enabled": False,
                "web_password": "admin-pass",
                "session_retain_days": 7,
            },
        )
        try:
            await asyncio.sleep(0)
            store = web_handler_module.GalgameWebHandler.user_store
            assert store is not None
            assert store.has_user("admin")
            assert store.verify("admin", "admin-pass")
            assert web_handler_module.GalgameWebHandler.auth_configured is True
            assert (
                web_handler_module.GalgameWebHandler.audio_owner_check
                is not None
            )
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
    assert web_handler_module.GalgameWebHandler.user_store is None
