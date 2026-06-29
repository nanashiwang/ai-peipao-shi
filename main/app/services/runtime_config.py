"""运行环境配置校验。

把开发、试点、正式环境的数据库和密钥策略集中在这里，避免生产误用本地默认配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit


PRODUCTION_ENVS = {"production", "prod"}
PILOT_ENVS = {"pilot", "staging", "docker"}
KNOWN_ENVS = {"local", "development", "dev", "test", "pilot", "staging", "docker", *PRODUCTION_ENVS}
PLACEHOLDER_KEYS = {"", "your-api-key", "sk-your-api-key", "changeme", "change-me"}


def normalize_app_env(value: str | None) -> str:
    return (value or "local").strip().lower() or "local"


def is_production_env(app_env: str | None) -> bool:
    return normalize_app_env(app_env) in PRODUCTION_ENVS


def database_kind(database_url: str) -> str:
    value = (database_url or "").strip()
    if value.startswith("sqlite"):
        return "sqlite"
    if value.startswith("postgresql"):
        return "postgresql"
    return value.split(":", 1)[0] or "unknown"


def mask_database_url(database_url: str) -> str:
    value = (database_url or "").strip()
    if not value:
        return ""
    if value.startswith("sqlite"):
        return value
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    user = parsed.username or ""
    auth = f"{user}:***@" if user else ""
    return f"{parsed.scheme}://{auth}{host}{port}{path}"


def read_ark_config_status(ark_config_path: Path) -> dict:
    if not ark_config_path.exists():
        return {"configured": False, "detail": "ARK 配置文件不存在"}
    try:
        data = json.loads(ark_config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"configured": False, "detail": f"ARK 配置无法解析：{exc}"}
    api_key = str(data.get("api_key") or "").strip()
    endpoint_id = str(data.get("endpoint_id") or "").strip()
    if api_key.lower() in PLACEHOLDER_KEYS or not endpoint_id:
        return {"configured": False, "detail": "ARK api_key 或 endpoint_id 未配置"}
    return {"configured": True, "detail": "ARK 已配置", "endpoint_id": endpoint_id}


def runtime_config_report(
    app_env: str | None,
    database_url: str,
    ark_config_path: Path,
    *,
    database_url_explicit: bool,
) -> dict:
    env = normalize_app_env(app_env)
    kind = database_kind(database_url)
    ark = read_ark_config_status(ark_config_path)
    critical: list[str] = []
    warnings: list[str] = []

    if env not in KNOWN_ENVS:
        warnings.append(f"未知 APP_ENV：{env}")
    if is_production_env(env):
        if not database_url_explicit:
            critical.append("正式环境必须显式设置 DATABASE_URL")
        if kind == "sqlite":
            critical.append("正式环境禁止使用 SQLite，本地库只能用于开发/试点")
        if not ark["configured"]:
            critical.append(f"正式环境必须配置独立 ARK 密钥：{ark['detail']}")
    elif env in PILOT_ENVS and kind == "sqlite":
        warnings.append("试点/部署环境正在使用 SQLite，扩容前应切换 PostgreSQL")

    status = "critical" if critical else ("warn" if warnings else "ok")
    detail = "；".join(critical or warnings) if (critical or warnings) else f"{env} 环境配置正常"
    return {
        "status": status,
        "label": "运行环境配置",
        "detail": detail,
        "metrics": {
            "app_env": env,
            "database_kind": kind,
            "database_url_masked": mask_database_url(database_url),
            "database_url_explicit": database_url_explicit,
            "ark_config_path": str(ark_config_path),
            "ark_configured": ark["configured"],
            "ark_detail": ark["detail"],
            "critical": critical,
            "warnings": warnings,
        },
    }


def assert_runtime_config_safe(report: dict) -> None:
    if report.get("status") == "critical":
        raise RuntimeError(f"运行环境配置不安全：{report.get('detail', '')}")
