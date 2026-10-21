from quart import request

from ..galgame_web.access import can_use, is_web_admin
from ..galgame_web.identity import (
    IDENTITY_SIG_HEADER,
    IDENTITY_TS_HEADER,
    IDENTITY_USER_HEADER,
    verify_identity,
)


class IdentityAPI:
    def _current_user(self) -> str | None:
        username = (request.headers.get(IDENTITY_USER_HEADER) or "").strip()
        if not username:
            username = self._default_user()
            return username if can_use(getattr(self, "config", {}), "web", username, admin=is_web_admin(self, username)) else None
        timestamp = request.headers.get(IDENTITY_TS_HEADER, "")
        signature = request.headers.get(IDENTITY_SIG_HEADER, "")
        secret = getattr(self, "_identity_secret", b"")
        if not secret or not verify_identity(
            secret, username, timestamp, signature
        ):
            return None
        store = getattr(self, "_user_store", None)
        if store is not None:
            username = store.normalize(username)
            if not store.has_user(username):
                return None
        return username if can_use(getattr(self, "config", {}), "web", username, admin=is_web_admin(self, username)) else None

    def _default_user(self) -> str:
        store = getattr(self, "_user_store", None)
        if store is not None:
            return store.default_owner() or "admin"
        return "admin"

    def _is_admin(self, username: str | None = None) -> bool:
        name = username if username is not None else self._current_user()
        return is_web_admin(self, name)


def resolve_owner(plugin) -> str | None:
    getter = getattr(plugin, "_current_user", None)
    if callable(getter):
        return getter()
    return "admin"


def session_audio_owned(plugin, owner: str, filename: str) -> bool:
    if not filename:
        return False
    for session in getattr(plugin, "_sessions", {}).values():
        if (session.get("owner") or "admin") != owner:
            continue
        for message in session.get("history", []):
            if isinstance(message, dict) and message.get("audio_file") == filename:
                return True
    loader = getattr(plugin, "_load_favorites", None)
    if callable(loader):
        try:
            for favorite in loader():
                if (
                    isinstance(favorite, dict)
                    and (favorite.get("owner") or "admin") == owner
                    and favorite.get("audio_file") == filename
                ):
                    return True
        except Exception:
            pass
    return False
