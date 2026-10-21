"""Moderated text snapshots, independent of private sessions and audio GC."""

import copy
import json
import threading
import time
import uuid

from .session_helpers import _DATA, atomic_write_json


class PoolStore:
    def __init__(self, path=None):
        self.path = path if path is not None else _DATA / "public_pool.json"
        self._lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("invalid public pool store")  # noqa: TRY004 -- corrupt JSON value
        return data

    def submit(self, owner, title, description, session):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError("title must be 1-100 characters")
        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError("description must be at most 2000 characters")
        history = []
        size = 0
        for message in session.get("history", []):
            if not isinstance(message, dict):
                continue
            role, content = message.get("role"), message.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            if not content.strip():
                continue
            size += len(content)
            history.append({"role": role, "content": content})
        if not history or len(history) > 400 or size > 100_000:
            raise ValueError("history must contain 1-400 messages and at most 100000 characters")
        item = {
            "id": uuid.uuid4().hex, "owner": owner, "title": title.strip(),
            "description": description.strip(), "history": history,
            "status": "pending", "created_at": time.time(),
            "reviewer": "", "reason": "", "reviewed_at": None,
        }
        with self._lock:
            items = self._load()
            if len(items) >= 1000:
                raise ValueError("public pool limit reached")
            if sum(i.get("owner") == owner and i.get("status") == "pending" for i in items) >= 10:
                raise ValueError("at most 10 pending submissions per account")
            items.append(item)
            atomic_write_json(self.path, items)
        return copy.deepcopy(item)

    def list_visible(self, user, admin=False):
        with self._lock:
            return [
                {**{k: v for k, v in item.items() if k != "history"},
                 "message_count": len(item.get("history", []))}
                for item in reversed(self._load())
                if admin or item.get("owner") == user or item.get("status") == "approved"
            ]

    def get_visible(self, item_id, user, admin=False):
        with self._lock:
            for item in self._load():
                if item.get("id") == item_id and (
                    admin or item.get("owner") == user or item.get("status") == "approved"
                ):
                    return copy.deepcopy(item)
        return None

    def review(self, item_id, reviewer, status, reason):
        if status not in ("approved", "rejected"):
            raise ValueError("status must be approved or rejected")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("reason must be at most 2000 characters")
        with self._lock:
            items = self._load()
            for item in items:
                if item.get("id") == item_id and item.get("status") == "pending":
                    item.update(status=status, reviewer=reviewer, reason=reason,
                                reviewed_at=time.time())
                    atomic_write_json(self.path, items)
                    return True
        return False

    def remove(self, item_id, user, admin=False):
        with self._lock:
            items = self._load()
            for item in items:
                if item.get("id") == item_id and (admin or item.get("owner") == user):
                    items.remove(item)
                    atomic_write_json(self.path, items)
                    return True
        return False
