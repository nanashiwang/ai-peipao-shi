"""运行环境配置校验。

把开发、试点、正式环境的数据库和密钥策略集中在这里，避免生产误用本地默认配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit


PRODUCTION_ENVS = {"production", "prod"}
PILOT_ENVS = {"pilot", "staging", "docker"}
DEPLOYED_ENVS = {*PILOT_ENVS, *PRODUCTION_ENVS}
KNOWN_ENVS = {"local", "development", "dev", "test", "pilot", "staging", "docker", *PRODUCTION_ENVS}
PLACEHOLDER_KEYS = {"", "your-api-key", "sk-your-api-key", "changeme", "change-me"}
PLACEHOLDER_SECRETS = {
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


def read_ark_config_status_from_env_or_file(ark_config_path: Path, env: dict | None = None) -> dict:
    source = env or {}
    api_key = str(source.get("ARK_API_KEY") or "").strip()
    endpoint_id = str(source.get("ARK_ENDPOINT_ID") or source.get("ARK_MODEL_NAME") or "").strip()
    if api_key or endpoint_id:
        if api_key.lower() in PLACEHOLDER_KEYS or not endpoint_id:
            return {"configured": False, "detail": "ARK 环境变量 api_key 或 endpoint_id 未配置完整", "source": "env"}
        return {"configured": True, "detail": "ARK 已通过环境变量配置", "endpoint_id": endpoint_id, "source": "env"}
    status = read_ark_config_status(ark_config_path)
    return {**status, "source": "file"}


def weak_secret_reason(name: str, value: str, min_length: int) -> str:
    clean = (value or "").strip()
    lowered = clean.lower()
    if lowered in PLACEHOLDER_SECRETS or "change-me" in lowered:
        return f"{name} 不能使用空值、默认值或占位符"
    if len(clean) < min_length:
        return f"{name} 至少需要 {min_length} 个字符"
    return ""


def database_password(database_url: str) -> str:
    if not (database_url or "").strip() or (database_url or "").startswith("sqlite"):
        return ""
    return urlsplit(database_url).password or ""


def explicit_bool_disabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def runtime_config_report(
    app_env: str | None,
    database_url: str,
    ark_config_path: Path,
    *,
    database_url_explicit: bool,
    env: dict | None = None,
) -> dict:
    source = env or {}
    env = normalize_app_env(app_env)
    kind = database_kind(database_url)
    ark = read_ark_config_status_from_env_or_file(ark_config_path, source)
    critical: list[str] = []
    warnings: list[str] = []

    if env not in KNOWN_ENVS:
        warnings.append(f"未知 APP_ENV：{env}")
    if env in DEPLOYED_ENVS:
        if explicit_bool_disabled(source.get("ADMIN_AUTH_REQUIRED")):
            critical.append("部署环境禁止设置 ADMIN_AUTH_REQUIRED=false")
        admin_secret = str(source.get("ADMIN_AUTH_SECRET") or "").strip()
        admin_reason = weak_secret_reason("ADMIN_AUTH_SECRET", admin_secret, 32)
        if admin_reason:
            critical.append(admin_reason)
        if kind == "sqlite":
            critical.append("部署环境禁止使用 SQLite，请配置 PostgreSQL")
        elif kind == "postgresql":
            db_reason = weak_secret_reason("数据库口令", database_password(database_url), 12)
            if db_reason:
                critical.append(db_reason)
    if str(source.get("WECOM_KF_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}:
        required_wecom_kf_callback = (
            "WECOM_KF_CORP_ID",
            "WECOM_KF_TOKEN",
            "WECOM_KF_ENCODING_AES_KEY",
        )
        missing_callback = [name for name in required_wecom_kf_callback if not str(source.get(name) or "").strip()]
        if missing_callback:
            critical.append(f"微信客服已启用但缺少回调配置：{', '.join(missing_callback)}")
        if not str(source.get("WECOM_KF_SECRET") or "").strip():
            warnings.append("微信客服回调可用，但缺少 WECOM_KF_SECRET，消息同步和发送尚未启用")
        aes_key = str(source.get("WECOM_KF_ENCODING_AES_KEY") or "").strip()
        if aes_key and len(aes_key) != 43:
            critical.append("WECOM_KF_ENCODING_AES_KEY 必须为43位")
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
            "ark_source": ark.get("source", "file"),
            "critical": critical,
            "warnings": warnings,
        },
    }


def assert_runtime_config_safe(report: dict) -> None:
    if report.get("status") == "critical":
        raise RuntimeError(f"运行环境配置不安全：{report.get('detail', '')}")
