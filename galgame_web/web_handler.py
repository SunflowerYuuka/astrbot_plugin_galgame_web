import base64
import hmac
import ipaddress
import json
import pathlib
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs

from astrbot.api import logger

from .access import can_use, members
from .identity import (
    IDENTITY_SIG_HEADER,
    IDENTITY_TS_HEADER,
    IDENTITY_USER_HEADER,
    sign_identity,
)
from .users import UserExistsError

PLUGIN_NAME = "astrbot_plugin_galgame_web"
PLUGIN_API_PREFIX = f"/api/plug/{PLUGIN_NAME}/"
MAX_LOGIN_BODY_BYTES = 4096
MAX_PROXY_REQUEST_BYTES = 45 * 1024 * 1024
MAX_PROXY_RESPONSE_BYTES = 64 * 1024 * 1024
LOGIN_WINDOW_SECONDS = 60
MAX_LOGIN_FAILURES = 8
MAX_SERVER_THREADS = 32
MAX_PROXY_CONCURRENCY = 8
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".vrm"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}

ALLOWED_API_ROUTES = {
    "GET": frozenset(
        {
            "assets/file",
            "assets/list",
            "audio/data",
            "bgm/data",
            "bgm/file",
            "bgm/list",
            "config",
            "favorites/list",
            "history",
            "me",
            "prefs",
            "session/list",
            "users/list",
            "pool/list",
            "pool/detail",
        }
    ),
    "POST": frozenset(
        {
            "assets/batch",
            "assets/batch-delete",
            "assets/copy",
            "assets/delete",
            "assets/upload",
            "assets/upload-key",
            "bgm/delete",
            "bgm/upload",
            "favorites/add",
            "favorites/delete",
            "prefs",
            "rapid_action",
            "send",
            "session/delete",
            "session/init",
            "users/add",
            "users/delete",
            "users/password",
            "pool/submit",
            "pool/review",
            "pool/claim",
            "pool/delete",
        }
    ),
}


def resolve_web_bind_host(
    configured_host: str, password: str, auth_configured: bool = False
) -> str:
    host = str(configured_host or "0.0.0.0").strip() or "0.0.0.0"
    if (
        not password
        and not auth_configured
        and host.lower() not in ("127.0.0.1", "localhost")
    ):
        return "127.0.0.1"
    return host


LOGIN_HTML = """\
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Galgame - 身份验证</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{display:flex;align-items:center;justify-content:center;min-height:100vh;
background:radial-gradient(ellipse at center,#141c2c 0%,#0a101c 100%);font-family:system-ui,-apple-system,sans-serif}
.login-box{width:min(360px,90vw);background:rgba(20,30,48,.85);border:1px solid rgba(140,170,210,.12);
border-radius:10px;padding:32px 28px;box-shadow:0 8px 48px rgba(0,0,0,.4)}
.login-box h1{font-size:18px;font-weight:700;color:rgba(219,231,245,.92);text-align:center;margin-bottom:6px}
.login-box p{font-size:13px;color:rgba(160,185,215,.5);text-align:center;margin-bottom:24px}
.login-box label{display:block;font-size:13px;color:rgba(170,195,225,.6);margin-bottom:6px}
.login-box input{width:100%;padding:10px 14px;font-size:15px;color:#dbe7f5;
background:rgba(255,255,255,.06);border:1px solid rgba(140,170,210,.15);border-radius:8px;outline:none;transition:border .2s}
.login-box input:focus{border-color:rgba(91,140,196,.45)}
.login-box input + label{margin-top:12px}
.login-box button{width:100%;margin-top:18px;padding:12px;font-size:15px;font-weight:600;color:#eaf2fb;
background:rgba(91,140,196,.35);border:1px solid rgba(140,170,210,.25);border-radius:8px;cursor:pointer;transition:background .2s}
.login-box button:hover{background:rgba(120,165,215,.45)}
.login-box .err{color:rgba(255,130,130,.7);font-size:13px;text-align:center;margin-top:12px;display:none}
</style>
</head>
<body>
<div class="login-box">
<h1>AI Galgame 虚拟伙伴</h1>
<p>请输入账号和密码</p>
<form method="post" action="/api/login">
<label for="username">账号</label>
<input id="username" type="text" name="username" autofocus autocapitalize="none" autocomplete="username" placeholder="输入账号..."/>
<label for="pwd">密码</label>
<input id="pwd" type="password" name="password" autocomplete="current-password" placeholder="输入密码..."/>
<button type="submit">进入</button>
<div class="err" id="err">账号或密码错误</div>
</form>
<!--REGISTER_LINK-->
</div>
<script>
var p=new URLSearchParams(location.search);
if(p.get("err")) document.getElementById("err").style.display="block";
</script>
</body>
</html>"""


def _safe_path(name: str, base_dir: pathlib.Path) -> pathlib.Path | None:
    if (
        not isinstance(name, str)
        or not name
        or "\x00" in name
        or "/" in name
        or "\\" in name
    ):
        return None
    candidate = pathlib.Path(name)
    if candidate.is_absolute() or candidate.name != name:
        return None
    base = base_dir.resolve()
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def _secret_bytes(secret: str | bytes) -> bytes:
    return secret if isinstance(secret, bytes) else secret.encode()


def _make_auth_token(secret: str | bytes, ts: int) -> str:
    h = hmac.new(_secret_bytes(secret), str(ts).encode(), "sha256").hexdigest()
    return f"{ts}:{h}"


def _make_user_auth_token(secret: str | bytes, username: str, ts: int) -> str:
    encoded = base64.urlsafe_b64encode(username.encode("utf-8")).decode("ascii")
    encoded = encoded.rstrip("=")
    message = f"{username}:{ts}".encode("utf-8")
    h = hmac.new(_secret_bytes(secret), message, "sha256").hexdigest()
    return f"{encoded}:{ts}:{h}"


def _check_auth_token(
    secret: str | bytes, token: str, max_age: int = 86400
) -> str | None:
    try:
        parts = token.split(":")
        if len(parts) == 2:
            username = "admin"
            ts = int(parts[0])
            signature = parts[1]
            message = str(ts).encode()
        elif len(parts) == 3:
            padding = "=" * (-len(parts[0]) % 4)
            username = base64.urlsafe_b64decode(parts[0] + padding).decode("utf-8")
            ts = int(parts[1])
            signature = parts[2]
            message = f"{username}:{ts}".encode("utf-8")
        else:
            return None
        age = int(time.time()) - ts
        if age < 0 or age > max_age:
            return None
        expected = hmac.new(
            _secret_bytes(secret), message, "sha256"
        ).hexdigest()
        return username if hmac.compare_digest(signature, expected) else None
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address, handler_class, max_workers=MAX_SERVER_THREADS):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def _canonical_plugin_api_path(raw_path: str, method: str) -> tuple[str, str] | None:
    """Return a normalized, allowlisted Dashboard path and query string."""
    parsed = urllib.parse.urlsplit(raw_path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    try:
        decoded = urllib.parse.unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if (
        "\\" in decoded
        or "\x00" in decoded
        or not decoded.startswith(PLUGIN_API_PREFIX)
    ):
        return None
    if any(segment in ("", ".", "..") for segment in decoded.split("/")[1:]):
        return None
    endpoint = decoded[len(PLUGIN_API_PREFIX) :]
    if endpoint not in ALLOWED_API_ROUTES.get(method, frozenset()):
        return None
    encoded_path = urllib.parse.quote(decoded, safe="/:@!$&'()*+,;=-._~")
    return encoded_path, parsed.query


class GalgameWebHandler(BaseHTTPRequestHandler):
    upstream = "http://127.0.0.1:6185"
    static_dir: pathlib.Path = pathlib.Path(__file__).parent / "galgame"
    assets_dir: pathlib.Path = (
        pathlib.Path("data/plugin_data") / "astrbot_plugin_galgame_web" / "assets"
    )
    audio_dir: pathlib.Path = (
        pathlib.Path("data/plugin_data") / "astrbot_plugin_galgame_web" / "audio"
    )
    bgm_dir: pathlib.Path = (
        pathlib.Path("data/plugin_data") / "astrbot_plugin_galgame_web" / "bgm"
    )
    jwt_token: str = ""
    jwt_token_factory = None
    web_password: str = ""
    user_store = None
    access_config: ClassVar[dict] = {}
    auth_configured: bool = False
    audio_owner_check = None
    auth_secret: bytes = secrets.token_bytes(32)
    _login_failures: dict[str, list[float]] = {}
    _login_lock = threading.Lock()
    _proxy_slots = threading.BoundedSemaphore(MAX_PROXY_CONCURRENCY)

    MIME = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "text/javascript",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".json": "application/json",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".opus": "audio/ogg",
    }

    def log_message(self, fmt, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        super().end_headers()

    def _send_json_error(self, status: int, message: str):
        data = (f'{{"error":"{message}"}}').encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, max_bytes: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self._send_json_error(400, "invalid content length")
            return None
        if length < 0:
            self._send_json_error(400, "invalid content length")
            return None
        if length > max_bytes:
            self._send_json_error(413, "request body too large")
            return None
        try:
            body = self.rfile.read(length) if length else b""
        except (OSError, TimeoutError):
            self._send_json_error(408, "request body timeout")
            return None
        if len(body) != length:
            self._send_json_error(400, "incomplete request body")
            return None
        return body

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        host = self.headers.get("Host", "").strip().lower()
        return (
            parsed.scheme in ("http", "https")
            and bool(host)
            and parsed.netloc.lower() == host
            and not parsed.path.strip("/")
            and not parsed.query
            and not parsed.fragment
        )

    def _host_ok(self) -> bool:
        if GalgameWebHandler.auth_configured or GalgameWebHandler.web_password:
            return True
        raw_host = self.headers.get("Host", "").strip()
        if not raw_host:
            return False
        try:
            hostname = urllib.parse.urlsplit(f"//{raw_host}").hostname
        except ValueError:
            return False
        return (hostname or "").lower() in ("127.0.0.1", "localhost", "::1")

    @staticmethod
    def _login_key(client_ip: str, username: str = "") -> str:
        name = str(username or "").strip().lower()
        return f"{name}:{client_ip}" if name else client_ip

    def _reserve_login_attempt(self, client_ip: str, username: str = "") -> bool:
        key = GalgameWebHandler._login_key(client_ip, username)
        now = time.monotonic()
        with GalgameWebHandler._login_lock:
            recent = [
                ts
                for ts in GalgameWebHandler._login_failures.get(key, [])
                if now - ts < LOGIN_WINDOW_SECONDS
            ]
            if len(recent) >= MAX_LOGIN_FAILURES:
                GalgameWebHandler._login_failures[key] = recent
                return False
            recent.append(now)
            GalgameWebHandler._login_failures[key] = recent
            return True

    def _clear_login_failures(self, client_ip: str, username: str = ""):
        key = self._login_key(client_ip, username)
        with GalgameWebHandler._login_lock:
            GalgameWebHandler._login_failures.pop(key, None)

    def _client_ip(self) -> str:
        remote = self.client_address[0] if self.client_address else "unknown"
        if remote in ("127.0.0.1", "::1"):
            candidate = (self.headers.get("CF-Connecting-IP") or "").strip()
            if candidate:
                try:
                    return str(ipaddress.ip_address(candidate))
                except ValueError:
                    pass
        return remote

    def _is_https(self) -> bool:
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if forwarded == "https":
            return True
        visitor = (self.headers.get("CF-Visitor") or "").lower()
        return '"https"' in visitor

    def _auth_ok(self) -> str | None:
        store = GalgameWebHandler.user_store
        has_users = bool(store and store.has_users())
        if not has_users and not GalgameWebHandler.web_password:
            if GalgameWebHandler.auth_configured:
                return None
            self.current_user = "admin"
            return "admin" if self._access_allowed("admin") else None
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("galgame_auth="):
                token = part.split("=", 1)[1].strip()
                break
        username = _check_auth_token(GalgameWebHandler.auth_secret, token)
        if not username:
            return None
        if has_users:
            username = store.normalize(username)
            if not store.has_user(username):
                return None
        elif username != "admin":
            return None
        if not self._access_allowed(username):
            return None
        self.current_user = username
        return username

    def _access_allowed(self, username):
        store = GalgameWebHandler.user_store
        config = GalgameWebHandler.access_config
        admin = username in members(config, "web_admins") or bool(store and store.is_admin(username))
        return can_use(config, "web", username, admin=admin)

    def _require_auth(self) -> bool:
        if self._auth_ok():
            return False
        path = self.path.split("?")[0]
        if path == "/login":
            return False
        if path == "/api/login":
            return False
        if path.startswith("/api/"):
            self._send_json_error(401, "authentication required")
            return True
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()
        return True

    def do_GET(self):
        if not self._host_ok():
            self._send_json_error(421, "invalid host")
            return
        path = self.path.split("?")[0]

        if path == "/login":
            return self._serve_login()
        if path in ("/register", "/register.html"):
            return self._serve_registration()
        if path.startswith("/api/"):
            if path in ("/api/login", "/api/register"):
                self.send_error(405)
                return
            if self._require_auth():
                return
            return self._proxy("GET")
        if self._require_auth():
            return
        return self._serve_static()

    def do_POST(self):
        if not self._host_ok():
            self._send_json_error(421, "invalid host")
            return
        path = self.path.split("?")[0]

        if not self._origin_ok():
            self._send_json_error(403, "cross-origin request rejected")
            return

        if path == "/api/login":
            return self._handle_login()
        if path == "/api/register":
            return self._handle_register()
        if path == "/api/logout":
            return self._handle_logout()
        if path.startswith("/api/"):
            if self._require_auth():
                return
            return self._proxy("POST")
        self.send_error(404)

    def _set_auth_cookie(self, token: str) -> str:
        cookie = (
            f"galgame_auth={token}; Path=/; HttpOnly; SameSite=Strict; "
            "Max-Age=86400"
        )
        if self._is_https():
            cookie += "; Secure"
        return cookie

    def _handle_login(self):
        client_ip = self._client_ip()
        raw_body = self._read_body(MAX_LOGIN_BODY_BYTES)
        if raw_body is None:
            return
        body = raw_body.decode("utf-8", errors="replace")
        params = parse_qs(body)
        username = (params.get("username", [""])[0] or "").strip().lower()[:64]
        submitted = params.get("password", [""])[0]
        if not self._reserve_login_attempt(client_ip, username):
            self._send_json_error(429, "too many login attempts")
            return

        store = GalgameWebHandler.user_store
        has_users = bool(store and store.has_users())
        authenticated = False
        if has_users:
            authenticated = store.verify(username, submitted)
            if authenticated:
                username = store.normalize(username)
        else:
            pwd = GalgameWebHandler.web_password
            authenticated = (
                bool(pwd)
                and username in ("", "admin")
                and hmac.compare_digest(submitted, pwd)
            )
            username = "admin"

        if authenticated and self._access_allowed(username):
            self._clear_login_failures(client_ip, username)
            token = _make_user_auth_token(
                GalgameWebHandler.auth_secret, username, int(time.time())
            )
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", self._set_auth_cookie(token))
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header("Location", "/login?err=1")
            self.end_headers()

    def _handle_logout(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            "galgame_auth=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _registration_ready(self):
        if GalgameWebHandler.access_config.get("web_registration_enabled", True) is not True:
            return 403, "管理员已关闭自助注册。"
        store = GalgameWebHandler.user_store
        if store is None or store.admin_count() == 0:
            return 503, "请管理员先设置 web_password 并重载插件，完成初始账号设置。"
        return None

    def _registration_response(self, status, data, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _handle_register(self):
        unavailable = self._registration_ready()
        if unavailable:
            return self._registration_response(unavailable[0], {"error": unavailable[1]})
        if not self._reserve_login_attempt(self._client_ip(), "@register"):
            return self._registration_response(429, {"error": "注册请求过于频繁，请一分钟后重试。"})
        raw_body = self._read_body(MAX_LOGIN_BODY_BYTES)
        if raw_body is None:
            return
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._registration_response(400, {"error": "注册信息格式不正确。"})
        if not isinstance(data, dict):
            return self._registration_response(400, {"error": "注册信息格式不正确。"})
        store = GalgameWebHandler.user_store
        username = data.get("username")
        password = data.get("password")
        if not isinstance(username, str):
            return self._registration_response(400, {"error": "账号必须为文字。"})
        try:
            username = store.validate_username(username)
        except ValueError:
            return self._registration_response(400, {"error": "账号须为 1–32 位字母、数字、下划线、点或短横线，并以字母开头。"})
        try:
            store.validate_password(password)
        except ValueError:
            return self._registration_response(400, {"error": "密码长度须为 8–256 位。"})
        if password != data.get("confirm_password"):
            return self._registration_response(400, {"error": "两次输入的密码不一致。"})
        config = GalgameWebHandler.access_config
        if username in {"admin"} | members(config, "web_admins"):
            return self._registration_response(403, {"error": "此账号名由管理员保留，请选择其他名称。"})
        if not can_use(config, "web", username, admin=False):
            return self._registration_response(403, {"error": "该账号不符合当前黑白名单规则，请联系管理员。"})
        try:
            store.add(username, password, is_admin=False)
        except UserExistsError:
            return self._registration_response(409, {"error": "该账号已存在，请登录或更换名称。"})
        except OSError:
            logger.exception("Failed to persist self-registration")
            return self._registration_response(500, {"error": "账号保存失败，请稍后重试。"})
        GalgameWebHandler.auth_configured = True
        token = _make_user_auth_token(GalgameWebHandler.auth_secret, username, int(time.time()))
        return self._registration_response(
            201, {"status": "ok", "username": username}, self._set_auth_cookie(token)
        )

    def _serve_registration(self):
        unavailable = self._registration_ready()
        if unavailable:
            return self._registration_response(unavailable[0], {"error": unavailable[1]})
        try:
            body = (self.static_dir / "register.html").read_bytes()
        except OSError:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_login(self):
        link = '' if self._registration_ready() else '<p style="margin:18px 0 0;text-align:center"><a href="/register" style="color:#9cc4ea">没有账号？注册账号</a></p>'
        html = LOGIN_HTML.replace("<!--REGISTER_LINK-->", link).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_static(self):
        path = urllib.parse.unquote(self.path.split("?")[0])
        if path == "/":
            path = "/index.html"
        filename = path.lstrip("/")
        safe = None
        if filename.startswith("assets/"):
            safe_assets = _safe_path(filename[len("assets/") :], self.assets_dir)
            if (
                safe_assets
                and safe_assets.suffix.lower() in ASSET_EXTS
                and safe_assets.is_file()
            ):
                safe = safe_assets
        elif filename.startswith("audio/"):
            safe_audio = _safe_path(filename[len("audio/") :], self.audio_dir)
            if (
                safe_audio
                and safe_audio.suffix.lower() in AUDIO_EXTS
                and safe_audio.is_file()
            ):
                checker = GalgameWebHandler.audio_owner_check
                if checker is not None and not checker(
                    getattr(self, "current_user", ""), safe_audio.name
                ):
                    self.send_error(404)
                    return
                safe = safe_audio
        elif filename.startswith("bgm/"):
            safe_bgm = _safe_path(filename[len("bgm/") :], self.bgm_dir)
            if (
                safe_bgm
                and safe_bgm.suffix.lower() in AUDIO_EXTS
                and safe_bgm.is_file()
            ):
                safe = safe_bgm
        else:
            safe = _safe_path(filename, self.static_dir)
        if not safe or not safe.is_file():
            self.send_error(404)
            return

        ext = safe.suffix.lower()
        mime = self.MIME.get(ext, "application/octet-stream")
        try:
            data = safe.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(500)

    def _proxy(self, method):
        if not GalgameWebHandler._proxy_slots.acquire(blocking=False):
            self._send_json_error(503, "server busy")
            return
        try:
            self._proxy_request(method)
        finally:
            GalgameWebHandler._proxy_slots.release()

    def _proxy_request(self, method):
        target = _canonical_plugin_api_path(self.path, method)
        if not target:
            self._send_json_error(404, "API route not found")
            return
        user = getattr(self, "current_user", "")
        if not user:
            self._send_json_error(401, "authentication required")
            return
        path, query = target
        url = self.upstream + path + (f"?{query}" if query else "")
        body = None
        if method == "POST":
            body = self._read_body(MAX_PROXY_REQUEST_BYTES)
            if body is None:
                return

        req = urllib.request.Request(url, data=body, method=method)
        for key in ("Accept", "Accept-Language", "User-Agent"):
            val = self.headers.get(key)
            if val:
                req.add_header(key, val)
        if body and method == "POST":
            req.add_header(
                "Content-Type", self.headers.get("Content-Type", "application/json")
            )
        token = GalgameWebHandler.jwt_token
        if GalgameWebHandler.jwt_token_factory:
            try:
                token = GalgameWebHandler.jwt_token_factory()
            except Exception as e:
                logger.warning(f"[proxy] failed to generate auth token: {e}")
                self._send_json_error(502, "proxy authentication unavailable")
                return
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        timestamp, signature = sign_identity(
            GalgameWebHandler.auth_secret, user
        )
        req.add_header(IDENTITY_USER_HEADER, user)
        req.add_header(IDENTITY_TS_HEADER, timestamp)
        req.add_header(IDENTITY_SIG_HEADER, signature)

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                status = resp.status
                body_bytes = resp.read(MAX_PROXY_RESPONSE_BYTES + 1)
                response_headers = list(resp.headers.items())
            if len(body_bytes) > MAX_PROXY_RESPONSE_BYTES:
                self._send_json_error(502, "upstream response too large")
                return

            self.send_response(status)
            for key, val in response_headers:
                low = key.lower()
                if low in (
                    "transfer-encoding",
                    "connection",
                    "keep-alive",
                    "content-length",
                    "access-control-allow-origin",
                    "access-control-allow-credentials",
                ):
                    continue
                self.send_header(key, val)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except urllib.error.HTTPError as e:
            logger.warning(
                f"[proxy] upstream HTTP error {e.code} for {method} {self.path}"
            )
            self.send_error(e.code or 502)
        except Exception as e:
            logger.warning(f"[proxy] upstream error for {method} {self.path}: {e}")
            self.send_error(502)
