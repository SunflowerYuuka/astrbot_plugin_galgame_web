# Multi-user Accounts Implementation Plan

> **Status:** implemented in v0.9.0; v0.10.0 adds access policies and a moderated public pool. The fork uses `main` for deployment.

**Goal:** Give every independent WebUI account its own sessions, voice favorites, and UI preferences, while keeping the existing single-password deployment upgradeable.

**Architecture:** Add a PBKDF2 account store (`users.json`) and a signed-cookie login. The standalone WebUI authenticates the browser, then forwards a short-lived signed identity header to the Dashboard API. All user-owned APIs resolve the current account from that header and filter data by an `owner` field.

**Tech Stack:** Python 3.12+, stdlib `hashlib`/`hmac`/`secrets`, Quart `request`, vanilla JS settings page.

**Spec:** This is a fork-local feature; upstream does not have a multi-user spec yet.

## Global Constraints

- Keep `metadata.yaml` `name: astrbot_plugin_galgame_web` unchanged so AstrBot treats this as the same plugin.
- Keep old `web_password` deployments working: first boot creates `admin` from `web_password`.
- Existing sessions, favorites, and prefs migrate to `admin` without data loss.
- Do not trust client-supplied identity headers on the standalone server; it must generate them from the authenticated cookie.
- Keep `favorites`/`prefs`/`sessions` owner-scoped; assets and BGM remain shared.
- Preserve the two upstream fixes: `Referrer-Policy: same-origin` and `enable_reasoning: False`.

## Data Model

- `data/plugin_data/astrbot_plugin_galgame_web/users.json`
  - `{version, users: {name: {salt, hash, iterations, is_admin, created_at, updated_at}}}`
- `sessions/<sid>.json` gains `owner`.
- `favorites.json` entries gain `owner`.
- `prefs_users.json` stores `{version, users: {name: prefs}}`; legacy `prefs.json` is the admin bootstrap value.
- `users.json` and `prefs_users.json` are written atomically; `users.json` is chmod 0600 where supported.

## Auth Flow

1. `/login` posts `username` + `password` to `/api/login`.
2. `web_handler` verifies against `UserStore` and sets `galgame_auth` (HMAC, username + timestamp, HttpOnly, SameSite=Strict, Secure on HTTPS).
3. Proxy requests to the Dashboard API with `Authorization: Bearer <dashboard JWT>` **and** signed `X-Galgame-User` / `X-Galgame-User-Ts` / `X-Galgame-User-Sig`.
4. `api/identity.py` verifies the signature (120s window) and checks the user still exists. Requests without identity headers (Dashboard embedded page) fall back to the default admin owner.
5. `api/session.py`, `favorites.py`, `prefs.py`, `config.py`, `audio.py` resolve the owner and enforce it.

## File Map

- Create: `galgame_web/users.py` — PBKDF2 account store and bootstrap.
- Create: `galgame_web/identity.py` — identity header sign/verify.
- Create: `api/identity.py` — `IdentityAPI`, `resolve_owner`, audio ownership helper.
- Create: `api/users.py` — admin account APIs and user-data purge.
- Create: `galgame_web/galgame/users.html` — standalone account management page.
- Modify: `galgame_web/web_handler.py` — username login, user tokens, identity headers, audio static ownership, logout, `Referrer-Policy`, allowlist.
- Modify: `main.py` — bootstrap admin, user store wiring, user API registration, legacy owner migration.
- Modify: `galgame_web/session_helpers.py` — session owner, per-user prefs, BGM pref cleanup.
- Modify: `api/session.py` — owner-scoped init/list/history/delete/send/rapid_action.
- Modify: `api/favorites.py` — owner-scoped list/add/delete and audio ownership.
- Modify: `api/prefs.py`, `api/config.py`, `api/audio.py` — per-user prefs and audio checks.
- Modify: `api/bgm.py` — deleting BGM clears every user's `bgm_file`.
- Modify: `galgame_web/pipeline.py` — `enable_reasoning: False`.
- Modify: `galgame_web/galgame/settings.html` — account management link.
- Docs: `README.md`, `AGENTS.md`, `CHANGELOG.md`, `metadata.yaml`, `_conf_schema.json`.

## Migration

- On startup, create `users.json` from non-empty `web_password` (`admin`).
- `load_all_sessions(..., legacy_owner=...)` writes `owner` into sessions that lack it.
- Legacy favorites have no owner and are treated as `admin`; they are written with an owner on the next save.
- Legacy `prefs.json` is returned for `admin` until the admin saves per-user prefs.
- Empty `web_password` + no users keeps the old localhost-only open mode.

## Test Coverage

- `test_users.py` — hashing, bootstrap, duplicate/invalid input.
- `test_identity.py` — signature validity/expiry/tampering, deleted users.
- `test_multi_user.py` — session/favorite/prefs isolation, legacy migration, admin CRUD, config owner.
- `test_web_accounts.py` — username login, cookie, identity headers, logout, `Referrer-Policy`.
- `test_plugin_bootstrap.py` — plugin startup bootstraps `admin` and wires the handler.
- `test_pipeline_payload.py` — reasoning/streaming flags.
- Existing 78 tests remain green; total 97.

## Verification

```bash
python -m pytest tests/ -q
python -m compileall -q api galgame_web main.py tests
git diff --check
```

## Deployment (AstrBot 4.29.1)

1. Push the verified changes to `main` in `SunflowerYuuka/astrbot_plugin_galgame_web`.
2. Back up `data/plugin_data/astrbot_plugin_galgame_web/` and the plugin config.
3. Install/update from `https://github.com/SunflowerYuuka/astrbot_plugin_galgame_web` (default branch `main`).
4. Log in with username `admin` and the old `web_password`.
5. Add accounts under 设置 → 账号管理.
6. Keep the Cloudflare Tunnel host unchanged; do not set `httpHostHeader` to `localhost:6886`.

## Known Limitations

- Dashboard embedded pages still act as the single Dashboard admin; only the standalone WebUI is multi-account.
- Assets, BGM, and character config are shared.
- Logout clears the browser cookie but does not revoke an already-issued token server-side; password changes do not invalidate old cookies (24h expiry).
- v0.10.1 adds optional self-service registration with immediate login; password resets remain managed by an admin.
- The fork repository URL tracks its default branch `main`; the upstream repository is a separate update source.
