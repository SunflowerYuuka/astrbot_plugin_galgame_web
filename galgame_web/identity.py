import hmac
import time

IDENTITY_USER_HEADER = "X-Galgame-User"
IDENTITY_TS_HEADER = "X-Galgame-User-Ts"
IDENTITY_SIG_HEADER = "X-Galgame-User-Sig"
IDENTITY_MAX_AGE_SECONDS = 120


def _secret_bytes(secret: str | bytes) -> bytes:
    return secret if isinstance(secret, bytes) else secret.encode("utf-8")


def sign_identity(
    secret: str | bytes, username: str, ts: int | None = None
) -> tuple[str, str]:
    timestamp = int(time.time() if ts is None else ts)
    message = f"{username}:{timestamp}".encode("utf-8")
    signature = hmac.new(_secret_bytes(secret), message, "sha256").hexdigest()
    return str(timestamp), signature


def verify_identity(
    secret: str | bytes,
    username: str,
    timestamp: str,
    signature: str,
    max_age: int = IDENTITY_MAX_AGE_SECONDS,
) -> bool:
    try:
        ts = int(str(timestamp).strip())
        age = int(time.time()) - ts
        if age < 0 or age > max_age:
            return False
        message = f"{username}:{ts}".encode("utf-8")
        expected = hmac.new(_secret_bytes(secret), message, "sha256").hexdigest()
        return hmac.compare_digest(str(signature or ""), expected)
    except (TypeError, ValueError):
        return False
