import json
import time
import uuid

from quart import request

from .identity import resolve_owner, session_audio_owned

MAX_FAVORITES = 500
MAX_FAVORITE_TEXT_CHARS = 20_000
ALLOWED_AUDIO_MIMES = {
    "audio/wav",
    "audio/mpeg",
    "audio/ogg",
    "audio/flac",
    "audio/mp4",
    "audio/aac",
}


class FavoritesAPI:
    def _register_favorites_apis(self):
        pn = self._plugin_name
        self.context.register_web_api(
            f"/{pn}/favorites/list",
            self._api_favorites_list,
            ["GET"],
            "List saved favorites",
        )
        self.context.register_web_api(
            f"/{pn}/favorites/add", self._api_favorites_add, ["POST"], "Add a favorite"
        )
        self.context.register_web_api(
            f"/{pn}/favorites/delete",
            self._api_favorites_delete,
            ["POST"],
            "Delete a favorite",
        )

    async def _api_favorites_list(self):
        owner = resolve_owner(self)
        if owner is None:
            return {"error": "authentication required"}, 401
        return {"favorites": self._load_favorites(owner)}

    async def _api_favorites_add(self):
        owner = resolve_owner(self)
        if owner is None:
            return {"error": "authentication required"}, 401
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        raw_text = data.get("text", "")
        raw_audio_file = data.get("audio_file", "")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        audio_file = raw_audio_file.strip() if isinstance(raw_audio_file, str) else ""
        audio_mime = data.get("audio_mime", "")
        if not text or not audio_file:
            return {"error": "text and audio_file required"}, 400
        if len(text) > MAX_FAVORITE_TEXT_CHARS:
            return {"error": "favorite text too long"}, 400
        if audio_mime not in ALLOWED_AUDIO_MIMES:
            return {"error": "unsupported audio type"}, 400
        from ..galgame_web.assets_helpers import safe_path
        from ..galgame_web.session_helpers import AUDIO_DIR

        audio_path = safe_path(audio_file, AUDIO_DIR)
        if not audio_path or not audio_path.is_file():
            return {"error": "audio file not found"}, 404
        if not session_audio_owned(self, owner, audio_file):
            return {"error": "audio file not found"}, 404
        favs = self._load_favorites()
        own_favs = [
            f for f in favs if (f.get("owner") or "admin") == owner
        ]
        if len(own_favs) >= MAX_FAVORITES:
            return {"error": "favorite limit reached"}, 429
        favs.insert(
            0,
            {
                "id": uuid.uuid4().hex,
                "owner": owner,
                "text": text,
                "audio_file": audio_file,
                "audio_mime": audio_mime,
                "saved_at": time.time(),
            },
        )
        self._save_favorites(favs)
        return {"status": "ok"}

    async def _api_favorites_delete(self):
        owner = resolve_owner(self)
        if owner is None:
            return {"error": "authentication required"}, 401
        data = await request.get_json() or {}
        if not isinstance(data, dict):
            return {"error": "invalid request"}, 400
        raw_fid = data.get("id", "")
        fid = raw_fid.strip() if isinstance(raw_fid, str) else ""
        if not fid:
            return {"error": "id required"}, 400
        favs = self._load_favorites()
        removed_favorites = [
            f
            for f in favs
            if f.get("id") == fid and (f.get("owner") or "admin") == owner
        ]
        kept = [
            f
            for f in favs
            if not (
                f.get("id") == fid and (f.get("owner") or "admin") == owner
            )
        ]
        if not removed_favorites:
            return {"error": "not found"}, 404
        self._save_favorites(kept)
        from ..galgame_web.session_helpers import cleanup_unreferenced_audio

        cleanup_unreferenced_audio(removed_favorites, self._sessions)
        return {"status": "ok"}

    def _load_favorites(self, owner: str | None = None) -> list[dict]:
        from ..galgame_web.assets_helpers import safe_path
        from ..galgame_web.session_helpers import AUDIO_DIR, FAVORITES_PATH

        if not FAVORITES_PATH.exists():
            return []
        try:
            favs = json.loads(FAVORITES_PATH.read_text(encoding="utf-8")) or []
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(favs, list):
            return []
        cleaned = []
        for favorite in favs:
            if not isinstance(favorite, dict):
                continue
            audio_path = safe_path(favorite.get("audio_file", ""), AUDIO_DIR)
            if audio_path and audio_path.is_file():
                cleaned.append(favorite)
        if len(cleaned) != len(favs):
            self._save_favorites(cleaned)
        if owner is not None:
            cleaned = [
                favorite
                for favorite in cleaned
                if (favorite.get("owner") or "admin") == owner
            ]
        return cleaned

    def _save_favorites(self, favs: list[dict]):
        from ..galgame_web.session_helpers import FAVORITES_PATH, atomic_write_json

        atomic_write_json(FAVORITES_PATH, favs)
