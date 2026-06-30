"""管理端认证与角色授权。

当前项目优先满足试点部署：本地默认不强制；正式环境通过 Bearer token 强制保护控制端 API。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass


ADMIN_ROLES = {"admin", "coach", "readonly"}
WRITE_ROLES = {"admin", "coach"}
ADMIN_ONLY_PREFIXES = (
    "/api/admin",
    "/api/audit-logs",
    "/api/ops",
    "/api/devices",
    "/api/ark-config",
    "/api/import",
    "/api/sample-data",
)
PUBLIC_PATHS = {
    "/health",
    "/api/admin/auth/login",
    "/api/test-chat/login",
}


@dataclass(frozen=True)
class AdminIdentity:
    username: str
    role: str
    display_name: str = ""
    exp: int = 0
    campus_names: tuple[str, ...] = ()


def admin_auth_required(env: dict | None = None) -> bool:
    source = env or os.environ
    explicit = str(source.get("ADMIN_AUTH_REQUIRED", "")).strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return str(source.get("APP_ENV", "")).strip().lower() in {"production", "prod"}


def admin_auth_secret(env: dict | None = None) -> str:
    source = env or os.environ
    secret = str(source.get("ADMIN_AUTH_SECRET", "")).strip()
    if admin_auth_required(source) and not secret:
        raise RuntimeError("正式管理端鉴权必须设置 ADMIN_AUTH_SECRET")
    return secret or "local-dev-admin-secret"


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


def sign_admin_token(
    username: str,
    role: str,
    display_name: str,
    secret: str,
    ttl_seconds: int = 8 * 3600,
    now: int | None = None,
    campus_names=None,
) -> str:
    if role not in ADMIN_ROLES:
        raise ValueError("非管理端角色不能签发控制端 token")
    issued_at = int(now if now is not None else time.time())
    payload = {
        "username": username,
        "role": role,
        "display_name": display_name,
        "exp": issued_at + int(ttl_seconds),
        "campus_names": list(normalize_campus_names(campus_names)),
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


def bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    value = authorization or ""
    return value[len(prefix):].strip() if value.startswith(prefix) else ""


def is_public_admin_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/api/send-artifacts/"):
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
