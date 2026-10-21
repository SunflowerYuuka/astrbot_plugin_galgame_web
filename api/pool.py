import asyncio
import time
import uuid

from astrbot.api import logger
from quart import request

from ..galgame_web.access import is_web_admin
from .identity import resolve_owner


class PoolAPI:
    def _register_pool_apis(self):
        for endpoint, handler, method in (
            ("list", self._api_pool_list, "GET"),
            ("detail", self._api_pool_detail, "GET"),
            ("submit", self._api_pool_submit, "POST"),
            ("review", self._api_pool_review, "POST"),
            ("claim", self._api_pool_claim, "POST"),
            ("delete", self._api_pool_delete, "POST"),
        ):
            self.context.register_web_api(
                f"/{self._plugin_name}/pool/{endpoint}", handler, [method],
                f"Public pool {endpoint}",
            )

    def _pool_admin(self, user):
        checker = getattr(self, "_is_admin", None)
        return checker(user) if callable(checker) else is_web_admin(self, user)

    async def _api_pool_list(self):
        user = resolve_owner(self)
        if not user:
            return {"error": "authentication required"}, 401
        return {"items": self._pool_store.list_visible(user, self._pool_admin(user))}

    async def _api_pool_detail(self):
        user = resolve_owner(self)
        if not user:
            return {"error": "authentication required"}, 401
        item = self._pool_store.get_visible(request.args.get("id", ""), user, self._pool_admin(user))
        return {"item": item} if item else ({"error": "not found"}, 404)

    async def _api_pool_submit(self):
        user = resolve_owner(self)
        if not user:
            return {"error": "authentication required"}, 401
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        sid = data.get("session_id")
        if not isinstance(sid, str):
            return {"error": "invalid session_id"}, 400
        session = self._sessions.get(sid)
        if not session or (session.get("owner") or "admin") != user:
            return {"error": "session not found"}, 404
        try:
            item = self._pool_store.submit(user, data.get("title"), data.get("description", ""), session)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"id": item["id"], "status": "pending"}

    async def _api_pool_review(self):
        user = resolve_owner(self)
        if not user:
            return {"error": "authentication required"}, 401
        if not self._pool_admin(user):
            return {"error": "admin permission required"}, 403
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        try:
            changed = self._pool_store.review(data.get("id"), user, data.get("status"), data.get("reason", ""))
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"status": "ok"} if changed else ({"error": "submission not pending"}, 409)

    async def _api_pool_delete(self):
        user = resolve_owner(self)
        if not user:
            return {"error": "authentication required"}, 401
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        changed = self._pool_store.remove(data.get("id"), user, self._pool_admin(user))
        return {"status": "ok"} if changed else ({"error": "not found"}, 404)

    async def _api_pool_claim(self):
        from ..galgame_web.session_helpers import (
            build_umo,
            delete_astrbot_conv,
            init_astrbot_conv,
            save_session,
            session_path,
        )
        from .session import MAX_SESSIONS

        user = resolve_owner(self)
        if not user:
            return {"error": "authentication required"}, 401
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        item = self._pool_store.get_visible(data.get("id"), user)
        if not item or item.get("status") != "approved":
            return {"error": "not found"}, 404
        if sum((s.get("owner") or "admin") == user for s in self._sessions.values()) >= MAX_SESSIONS:
            return {"error": "session limit reached"}, 429
        sid = uuid.uuid4().hex
        session = {
            "owner": user, "umo": build_umo(self._webchat_username, sid),
            "conv_id": "", "history": [
                {**message, "id": uuid.uuid4().hex} for message in item["history"]
            ], "created_at": time.time(), "current_emotion": "neutral",
            "pending_rapid_clicks": 0, "_lock": asyncio.Lock(),
            "_send_lock": asyncio.Lock(), "_resp_event": asyncio.Event(),
            "_audio_event": asyncio.Event(),
        }
        self._sessions[sid] = session
        if not save_session(self._sessions, sid):
            self._sessions.pop(sid, None)
            return {"error": "failed to save session"}, 500
        try:
            await init_astrbot_conv(self.context, self._webchat_username, self.config, sid, session)
            if not session.get("conv_id"):
                raise RuntimeError("conversation initialization failed")
            if not save_session(self._sessions, sid):
                raise RuntimeError("failed to persist conversation")
        except Exception:  # noqa: BLE001 -- rollback across AstrBot database adapters
            logger.exception("Failed to initialize a claimed public conversation")
            self._sessions.pop(sid, None)
            session_path(sid).unlink(missing_ok=True)
            await delete_astrbot_conv(self.context, self._webchat_username, sid)
            return {"error": "failed to initialize conversation"}, 502
        return {"session_id": sid}
