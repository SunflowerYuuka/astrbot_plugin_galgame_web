"""Access policy shared by bot commands, WebUI login and plugin APIs."""


def members(config: dict, key: str) -> set[str]:
    values = config.get(key, [])
    if not isinstance(values, (list, tuple)):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def can_use(
    config: dict, scope: str, identity: str, *, admin: bool = False,
    platform: str = "",
) -> bool:
    identities = {str(identity).strip()}
    if platform:
        identities.add(f"{platform}:{identity}")
    if identities & members(config, f"{scope}_blacklist"):
        return False
    mode = config.get(f"{scope}_access_mode", "blacklist")
    if mode == "whitelist":
        return admin or bool(identities & members(config, f"{scope}_whitelist"))
    return True


def is_web_admin(plugin, username: str) -> bool:
    store = getattr(plugin, "_user_store", None)
    return bool(
        username
        and (username in members(getattr(plugin, "config", {}), "web_admins")
             or (store and store.is_admin(username)))
    )
