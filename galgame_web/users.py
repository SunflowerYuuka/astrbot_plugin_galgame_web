import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import threading
import time

from astrbot.api import logger
from astrbot.api.star import StarTools

from .session_helpers import atomic_write_json

_PLUGIN = "astrbot_plugin_galgame_web"
_DATA = StarTools.get_data_dir(_PLUGIN)
USERS_PATH = _DATA / "users.json"

USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
PBKDF2_ITERATIONS = 210_000


class UserStore:
    def __init__(self, path: pathlib.Path | None = None):
        self.path = pathlib.Path(path) if path is not None else USERS_PATH
        self._lock = threading.RLock()

    @staticmethod
    def normalize(username: object) -> str:
        return str(username or "").strip().lower()

    @classmethod
    def validate_username(cls, username: object) -> str:
        normalized = cls.normalize(username)
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "username must be 1-32 chars: lowercase letters, digits, '_', '.', '-'"
            )
        return normalized

    @staticmethod
    def validate_password(password: object, allow_short: bool = False) -> str:
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError("password too long")
        if not allow_short and len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} chars")
        if not password:
            raise ValueError("password required")
        return password

    @staticmethod
    def _hash_password(
        password: str,
        salt: bytes | None = None,
        iterations: int = PBKDF2_ITERATIONS,
    ) -> tuple[str, str, int]:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return salt.hex(), digest.hex(), iterations

    def _empty(self) -> dict:
        return {"version": 1, "users": {}}

    def _load(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            logger.warning(f"Failed to read user store: {exc}")
            return self._empty()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("User store is not valid JSON; ignoring it")
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        users = data.get("users")
        if not isinstance(users, dict):
            users = {}
            data["users"] = users
        data.setdefault("version", 1)
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, data)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def has_users(self) -> bool:
        return bool(self._load()["users"])

    def has_user(self, username: object) -> bool:
        name = self.normalize(username)
        return bool(name) and name in self._load()["users"]

    def is_admin(self, username: object) -> bool:
        user = self._load()["users"].get(self.normalize(username))
        return bool(isinstance(user, dict) and user.get("is_admin"))

    def admin_count(self) -> int:
        return sum(
            1
            for user in self._load()["users"].values()
            if isinstance(user, dict) and user.get("is_admin")
        )

    def default_owner(self) -> str:
        users = self._load()["users"]
        if "admin" in users:
            return "admin"
        for name, user in users.items():
            if isinstance(user, dict) and user.get("is_admin"):
                return name
        return next(iter(users), "")

    def list_users(self) -> list[dict]:
        users = self._load()["users"]
        result = []
        for name, user in users.items():
            if not isinstance(user, dict):
                continue
            result.append(
                {
                    "username": name,
                    "is_admin": bool(user.get("is_admin")),
                    "created_at": user.get("created_at", 0),
                    "updated_at": user.get("updated_at", 0),
                }
            )
        result.sort(key=lambda item: (not item["is_admin"], item["username"]))
        return result

    def verify(self, username: object, password: object) -> bool:
        name = self.normalize(username)
        if not name or not isinstance(password, str):
            return False
        user = self._load()["users"].get(name)
        if not isinstance(user, dict):
            return False
        try:
            salt = bytes.fromhex(str(user.get("salt", "")))
            expected = bytes.fromhex(str(user.get("hash", "")))
            iterations = int(user.get("iterations", PBKDF2_ITERATIONS))
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, iterations
            )
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(digest, expected)

    def add(
        self, username: object, password: object, is_admin: bool = False
    ) -> dict:
        name = self.validate_username(username)
        password = self.validate_password(password)
        with self._lock:
            data = self._load()
            if name in data["users"]:
                raise ValueError("user already exists")
            salt, digest, iterations = self._hash_password(password)
            now = time.time()
            data["users"][name] = {
                "salt": salt,
                "hash": digest,
                "iterations": iterations,
                "is_admin": bool(is_admin),
                "created_at": now,
                "updated_at": now,
            }
            self._save(data)
        return {"username": name, "is_admin": bool(is_admin)}

    def delete(self, username: object) -> bool:
        name = self.normalize(username)
        with self._lock:
            data = self._load()
            if name not in data["users"]:
                return False
            del data["users"][name]
            self._save(data)
        return True

    def set_password(self, username: object, password: object) -> bool:
        name = self.normalize(username)
        password = self.validate_password(password)
        with self._lock:
            data = self._load()
            user = data["users"].get(name)
            if not isinstance(user, dict):
                return False
            salt, digest, iterations = self._hash_password(password)
            user["salt"] = salt
            user["hash"] = digest
            user["iterations"] = iterations
            user["updated_at"] = time.time()
            self._save(data)
        return True

    def ensure_bootstrap(self, password: object) -> str | None:
        if self.has_users():
            return None
        if not isinstance(password, str) or not password:
            return None
        password = self.validate_password(password, allow_short=True)
        with self._lock:
            data = self._load()
            if data["users"]:
                return None
            salt, digest, iterations = self._hash_password(password)
            now = time.time()
            data["users"]["admin"] = {
                "salt": salt,
                "hash": digest,
                "iterations": iterations,
                "is_admin": True,
                "created_at": now,
                "updated_at": now,
            }
            self._save(data)
        return "admin"
