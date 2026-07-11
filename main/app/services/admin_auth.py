"""管理端认证与角色授权。

当前项目优先满足试点部署：本地默认不强制；正式环境通过 Bearer token 强制保护控制端 API。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path


ADMIN_ROLES = {"admin", "coach", "readonly"}
WRITE_ROLES = {"admin", "coach"}
DEPLOYED_ENVS = {"pilot", "staging", "docker", "production", "prod"}
WEAK_SECRET_VALUES = {
    "",
    "admin",
    "change-me",
    "change-me-before-production",
    "changeme",
    "coach",
    "local-dev-admin-secret",
    "password",
    "secret",
}
ADMIN_ONLY_PREFIXES = (
    "/api/admin",
    "/api/audit-logs",
    "/api/ops",
    "/api/devices",
    "/api/ark-config",
    "/api/import",
    "/api/sample-data",
)
DEFAULT_ADMIN_TOKEN_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_PARENT_TOKEN_TTL_SECONDS = 30 * 24 * 3600
MIN_TOKEN_TTL_SECONDS = 3600
MAX_TOKEN_TTL_SECONDS = 365 * 24 * 3600
PUBLIC_PATHS = {
    "/health",
    "/api/wecom-kf/callback",
    "/api/wecom-customer/callback",
    "/api/admin/auth/status",
    "/api/admin/auth/login",
    "/api/admin/auth/register",
}


@dataclass(frozen=True)
class AdminIdentity:
    username: str
    role: str
    display_name: str = ""
    exp: int = 0
    campus_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParentIdentity:
    username: str
    display_name: str
    family_id: str
    exp: int = 0


def admin_auth_required(env: dict | None = None) -> bool:
    source = env or os.environ
    explicit = str(source.get("ADMIN_AUTH_REQUIRED", "")).strip().lower()
    app_env = str(source.get("APP_ENV", "")).strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return app_env in DEPLOYED_ENVS
    return app_env in DEPLOYED_ENVS


def weak_admin_secret_reason(secret: str) -> str:
    value = (secret or "").strip()
    lowered = value.lower()
    if lowered in WEAK_SECRET_VALUES or "change-me" in lowered:
        return "ADMIN_AUTH_SECRET 不能使用空值、默认值或占位符"
    if len(value) < 32:
        return "ADMIN_AUTH_SECRET 至少需要 32 个字符"
    return ""


def admin_auth_secret(env: dict | None = None) -> str:
    source = env or os.environ
    secret = str(source.get("ADMIN_AUTH_SECRET", "")).strip()
    app_env = str(source.get("APP_ENV", "")).strip().lower()
    if secret:
        if app_env in DEPLOYED_ENVS:
            reason = weak_admin_secret_reason(secret)
            if reason:
                raise RuntimeError(reason)
        return secret
    if admin_auth_required(source) and app_env in DEPLOYED_ENVS:
        raise RuntimeError("部署环境必须显式设置强随机 ADMIN_AUTH_SECRET")
    if env is not None:
        return "local-dev-admin-secret"
    return persisted_dev_secret()


def persisted_dev_secret() -> str:
    path = Path(os.getenv("ADMIN_AUTH_SECRET_FILE", "config/admin_secret.txt"))
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    return value


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def normalize_campus_names(value) -> tuple[str, ...]:
    if not value:
        return ()
    raw_items = value if isinstance(value, (list, tuple, set)) else str(value).replace("，", ",").split(",")
    names: list[str] = []
    for item in raw_items:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _token_ttl_seconds(env: dict | None, name: str, default: int) -> int:
    source = env or os.environ
    try:
        value = int(str(source.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return min(max(value, MIN_TOKEN_TTL_SECONDS), MAX_TOKEN_TTL_SECONDS)


def admin_token_ttl_seconds(env: dict | None = None) -> int:
    return _token_ttl_seconds(env, "ADMIN_TOKEN_TTL_SECONDS", DEFAULT_ADMIN_TOKEN_TTL_SECONDS)


def parent_token_ttl_seconds(env: dict | None = None) -> int:
    return _token_ttl_seconds(env, "PARENT_TOKEN_TTL_SECONDS", DEFAULT_PARENT_TOKEN_TTL_SECONDS)


def sign_admin_token(
    username: str,
    role: str,
    display_name: str,
    secret: str,
    ttl_seconds: int | None = None,
    now: int | None = None,
    campus_names=None,
) -> str:
    if role not in ADMIN_ROLES:
        raise ValueError("非管理端角色不能签发控制端 token")
    issued_at = int(now if now is not None else time.time())
    ttl = int(ttl_seconds if ttl_seconds is not None else admin_token_ttl_seconds())
    payload = {
        "username": username,
        "role": role,
        "display_name": display_name,
        "exp": issued_at + ttl,
        "campus_names": list(normalize_campus_names(campus_names)),
    }
    payload_b64 = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def sign_parent_token(username: str, display_name: str, family_id: str, secret: str, ttl_seconds: int | None = None, now: int | None = None) -> str:
    issued_at = int(now if now is not None else time.time())
    ttl = int(ttl_seconds if ttl_seconds is not None else parent_token_ttl_seconds())
    payload = {
        "username": username,
        "role": "parent",
        "display_name": display_name,
        "family_id": family_id,
        "exp": issued_at + ttl,
    }
    payload_b64 = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def verify_admin_token(token: str, secret: str, now: int | None = None) -> AdminIdentity:
    try:
        payload_b64, sig_b64 = (token or "").split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(expected), sig_b64):
            raise ValueError("签名不匹配")
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise ValueError("管理端 token 无效") from exc
    role = str(payload.get("role", ""))
    exp = int(payload.get("exp", 0))
    if role not in ADMIN_ROLES:
        raise ValueError("管理端角色无效")
    if exp < int(now if now is not None else time.time()):
        raise ValueError("管理端 token 已过期")
    return AdminIdentity(
        username=str(payload.get("username", "")),
        role=role,
        display_name=str(payload.get("display_name", "")),
        exp=exp,
        campus_names=normalize_campus_names(payload.get("campus_names")),
    )


def verify_parent_token(token: str, secret: str, now: int | None = None) -> ParentIdentity:
    try:
        payload_b64, sig_b64 = (token or "").split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(expected), sig_b64):
            raise ValueError("签名不匹配")
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise ValueError("家长端 token 无效") from exc
    exp = int(payload.get("exp", 0))
    family_id = str(payload.get("family_id", "")).strip()
    if payload.get("role") != "parent" or not family_id:
        raise ValueError("家长端 token 角色无效")
    if exp < int(now if now is not None else time.time()):
        raise ValueError("家长端 token 已过期")
    return ParentIdentity(
        username=str(payload.get("username", "")),
        display_name=str(payload.get("display_name", "")),
        family_id=family_id,
        exp=exp,
    )


def bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    value = authorization if isinstance(authorization, str) else ""
    return value[len(prefix):].strip() if value.startswith(prefix) else ""


def is_public_admin_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/api/parent/"):
        return True
    if path.startswith("/static/"):
        return True
    return False


def is_device_or_rpa_path(path: str) -> bool:
    if path.startswith("/api/rpa/"):
        return True
    if path.startswith("/api/devices/") and (path.endswith("/heartbeat") or path.endswith("/claim")):
        return True
    if path.startswith("/api/send-tasks/") and path.endswith("/result"):
        return True
    return False


def path_requires_admin_auth(path: str) -> bool:
    return path.startswith("/api/") and not is_public_admin_path(path) and not is_device_or_rpa_path(path)


def role_allowed_for_request(role: str, method: str, path: str) -> bool:
    clean_role = role or ""
    if clean_role not in ADMIN_ROLES:
        return False
    if path.startswith("/api/admin/auth/"):
        return True
    if any(path.startswith(prefix) for prefix in ADMIN_ONLY_PREFIXES):
        return clean_role == "admin"
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return True
    return clean_role in WRITE_ROLES
