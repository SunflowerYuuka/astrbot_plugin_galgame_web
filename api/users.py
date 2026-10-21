from astrbot.api import logger
from quart import request

from ..galgame_web.access import is_web_admin
from .identity import resolve_owner


class UserAPI:
    def _register_user_apis(self):
        pn = self._plugin_name
        self.context.register_web_api(
            f"/{pn}/me", self._api_me, ["GET"], "Get the current account"
        )
        self.context.register_web_api(
            f"/{pn}/users/list",
            self._api_users_list,
            ["GET"],
            "List accounts (admin only)",
        )
        self.context.register_web_api(
            f"/{pn}/users/add",
            self._api_users_add,
            ["POST"],
            "Create an account (admin only)",
        )
        self.context.register_web_api(
            f"/{pn}/users/delete",
            self._api_users_delete,
            ["POST"],
            "Delete an account and its data (admin only)",
        )
        self.context.register_web_api(
            f"/{pn}/users/password",
            self._api_users_password,
            ["POST"],
            "Reset an account password (admin only)",
        )

    def _admin_context(self):
        username = resolve_owner(self)
        if username is None:
            return None, ({"error": "authentication required"}, 401)
        store = getattr(self, "_user_store", None)
        if store is None:
            return None, ({"error": "user store unavailable"}, 503)
        if not is_web_admin(self, username):
            return None, ({"error": "admin permission required"}, 403)
        return (username, store), None

    async def _api_me(self):
        username = resolve_owner(self)
        if username is None:
            return {"error": "authentication required"}, 401
        is_admin = is_web_admin(self, username)
        return {"username": username, "is_admin": is_admin}

    async def _api_users_list(self):
        ctx, error = self._admin_context()
        if error:
            return error
        _, store = ctx
        return {"users": store.list_users()}

    async def _api_users_add(self):
        ctx, error = self._admin_context()
        if error:
            return error
        _, store = ctx
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        username = data.get("username", "")
        password = data.get("password", "")
        is_admin = data.get("is_admin", False) is True
        try:
            user = store.add(username, password, is_admin=is_admin)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"status": "ok", "user": user}

    async def _api_users_delete(self):
        ctx, error = self._admin_context()
        if error:
            return error
        current_user, store = ctx
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        username = store.normalize(data.get("username", ""))
        if not username or not store.has_user(username):
            return {"error": "user not found"}, 404
        if store.is_admin(username) and store.admin_count() <= 1:
            return {"error": "cannot delete the last admin"}, 400
        removed = await self._purge_user_data(username)
        if not store.delete(username):
            return {"error": "failed to delete user"}, 500
        logger.info(f"Deleted account {username} (removed {removed})")
        return {"status": "ok", "removed": removed, "current_user": current_user}

    async def _api_users_password(self):
        ctx, error = self._admin_context()
        if error:
            return error
        _, store = ctx
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        username = data.get("username", "")
        password = data.get("password", "")
        try:
            if not store.has_user(username):
                return {"error": "user not found"}, 404
            store.set_password(username, password)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"status": "ok"}

    async def _purge_user_data(self, username: str) -> dict:
        from ..galgame_web.session_helpers import (
            SESSIONS_DIR,
            cleanup_unreferenced_audio,
            delete_astrbot_conv,
            delete_prefs,
            is_valid_session_id,
            load_session,
            session_path,
        )

        removed = {"sessions": 0, "favorites": 0, "prefs": 0}
        removed_history = []
        session_ids = []

        for sid, session in list(getattr(self, "_sessions", {}).items()):
            if (session.get("owner") or "admin") != username:
                continue
            removed_history.extend(session.get("history", []))
            session_ids.append(sid)
            self._sessions.pop(sid, None)
            path = session_path(sid)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    logger.warning(f"Failed to delete session file {sid}")
            removed["sessions"] += 1

        for path in SESSIONS_DIR.glob("*.json"):
            sid = path.stem
            if not is_valid_session_id(sid) or sid in session_ids:
                continue
            stored = load_session(sid)
            if not stored or (stored.get("owner") or "admin") != username:
                continue
            removed_history.extend(stored.get("history", []))
            session_ids.append(sid)
            try:
                path.unlink()
            except OSError:
                logger.warning(f"Failed to delete session file {sid}")
            removed["sessions"] += 1

        for sid in session_ids:
            try:
                await delete_astrbot_conv(
                    self.context, self._webchat_username, sid
                )
            except Exception:
                logger.warning(f"Failed to delete conversation for {sid}")

        if removed_history:
            cleanup_unreferenced_audio(removed_history, getattr(self, "_sessions", {}))

        favorites = self._load_favorites()
        kept = []
        removed_favorites = []
        for favorite in favorites:
            if (favorite.get("owner") or "admin") == username:
                removed_favorites.append(favorite)
            else:
                kept.append(favorite)
        if removed_favorites:
            self._save_favorites(kept)
            cleanup_unreferenced_audio(
                removed_favorites, getattr(self, "_sessions", {})
            )
            removed["favorites"] = len(removed_favorites)

        if delete_prefs(username):
            removed["prefs"] = 1

        return removed
