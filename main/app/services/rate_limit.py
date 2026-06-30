"""管理端接口限流。

基础版使用进程内滑动窗口，优先保护登录和控制端 API，避免误操作或脚本刷爆本地试点服务。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from app.services.admin_auth import path_requires_admin_auth


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "")).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    rule: str
    remaining: int
    retry_after_seconds: int = 0


class SlidingWindowRateLimiter:
    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, rule: RateLimitRule, now: float | None = None) -> RateLimitDecision:
        current = time.monotonic() if now is None else now
        window_start = current - rule.window_seconds
        bucket_key = f"{rule.name}:{key}"
        with self._lock:
            bucket = self._hits[bucket_key]
            while bucket and bucket[0] <= window_start:
                bucket.popleft()
            if len(bucket) >= rule.limit:
                retry_after = max(1, int(bucket[0] + rule.window_seconds - current) + 1)
                return RateLimitDecision(False, rule.name, 0, retry_after)
            bucket.append(current)
            return RateLimitDecision(True, rule.name, rule.limit - len(bucket), 0)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def bucket_count(self) -> int:
        with self._lock:
            return len(self._hits)


ADMIN_API_RULE = RateLimitRule(
    "admin_api",
    _env_int("ADMIN_API_RATE_LIMIT", 240),
    _env_int("ADMIN_API_RATE_WINDOW_SECONDS", 60),
)
ADMIN_LOGIN_RULE = RateLimitRule(
    "admin_login",
    _env_int("ADMIN_LOGIN_RATE_LIMIT", 10),
    _env_int("ADMIN_LOGIN_RATE_WINDOW_SECONDS", 60),
)

admin_rate_limiter = SlidingWindowRateLimiter()


def rate_limit_enabled() -> bool:
    return _env_bool("ADMIN_RATE_LIMIT_ENABLED", True)


def admin_rate_limit_rule_for_path(path: str) -> RateLimitRule | None:
    if path == "/api/admin/auth/login":
        return ADMIN_LOGIN_RULE
    if path_requires_admin_auth(path):
        return ADMIN_API_RULE
    return None


def rate_limit_key_for_request(client_host: str | None, rule: RateLimitRule) -> str:
    host = (client_host or "unknown").strip() or "unknown"
    group = "login" if rule.name == ADMIN_LOGIN_RULE.name else "api"
    return f"{host}:{group}"


def rate_limit_report() -> dict:
    enabled = rate_limit_enabled()
    detail = (
        f"已启用：API {ADMIN_API_RULE.limit}/{ADMIN_API_RULE.window_seconds}s，"
        f"登录 {ADMIN_LOGIN_RULE.limit}/{ADMIN_LOGIN_RULE.window_seconds}s"
        if enabled
        else "未启用"
    )
    return {
        "status": "ok" if enabled else "warn",
        "label": "管理端限流",
        "detail": detail,
        "metrics": {
            "enabled": enabled,
            "active_buckets": admin_rate_limiter.bucket_count(),
            "api_limit": ADMIN_API_RULE.limit,
            "api_window_seconds": ADMIN_API_RULE.window_seconds,
            "login_limit": ADMIN_LOGIN_RULE.limit,
            "login_window_seconds": ADMIN_LOGIN_RULE.window_seconds,
        },
    }
