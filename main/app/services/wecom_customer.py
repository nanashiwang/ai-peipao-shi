"""企业微信客户联系 API：客户同步、事件回调、欢迎语和企业群发任务。"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from app.services.wecom_kf import (
    decrypt_callback_value,
    verify_callback_signature,
    xml_value,
)


WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


class WecomCustomerApiError(RuntimeError):
    def __init__(self, message: str, errcode: int = -1):
        super().__init__(message)
        self.errcode = errcode


@dataclass(frozen=True)
class WecomCustomerConfig:
    enabled: bool
    corp_id: str
    secret: str
    token: str
    encoding_aes_key: str
    sync_enabled: bool = True
    dispatch_enabled: bool = True
    worker_interval_seconds: int = 30
    sync_interval_seconds: int = 300
    welcome_enabled: bool = False
    welcome_text: str = "您好，已经收到您的添加。后续课程安排、任务提醒和学习反馈会通过这里与您同步。"


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def read_wecom_customer_config() -> WecomCustomerConfig:
    return WecomCustomerConfig(
        enabled=_env_bool("WECOM_CUSTOMER_ENABLED", False),
        corp_id=os.getenv("WECOM_CUSTOMER_CORP_ID", "").strip(),
        secret=os.getenv("WECOM_CUSTOMER_SECRET", "").strip(),
        token=os.getenv("WECOM_CUSTOMER_TOKEN", "").strip(),
        encoding_aes_key=os.getenv("WECOM_CUSTOMER_ENCODING_AES_KEY", "").strip(),
        sync_enabled=_env_bool("WECOM_CUSTOMER_SYNC_ENABLED", True),
        dispatch_enabled=_env_bool("WECOM_CUSTOMER_DISPATCH_ENABLED", True),
        worker_interval_seconds=_env_int("WECOM_CUSTOMER_WORKER_INTERVAL_SECONDS", 30, 5, 300),
        sync_interval_seconds=_env_int("WECOM_CUSTOMER_SYNC_INTERVAL_SECONDS", 300, 30, 3600),
        welcome_enabled=_env_bool("WECOM_CUSTOMER_WELCOME_ENABLED", False),
        welcome_text=os.getenv(
            "WECOM_CUSTOMER_WELCOME_TEXT",
            "您好，已经收到您的添加。后续课程安排、任务提醒和学习反馈会通过这里与您同步。",
        ).strip(),
    )


def callback_config_status(config: WecomCustomerConfig | None = None) -> dict:
    cfg = config or read_wecom_customer_config()
    required = {
        "WECOM_CUSTOMER_CORP_ID": cfg.corp_id,
        "WECOM_CUSTOMER_TOKEN": cfg.token,
        "WECOM_CUSTOMER_ENCODING_AES_KEY": cfg.encoding_aes_key,
    }
    missing = [name for name, value in required.items() if not value]
    if cfg.encoding_aes_key and len(cfg.encoding_aes_key) != 43:
        missing.append("WECOM_CUSTOMER_ENCODING_AES_KEY(必须43位)")
    return {
        "callback_configured": cfg.enabled and not missing,
        "callback_missing": missing,
    }


def config_status(config: WecomCustomerConfig | None = None) -> dict:
    cfg = config or read_wecom_customer_config()
    callback = callback_config_status(cfg)
    missing = list(callback["callback_missing"])
    if not cfg.secret:
        missing.append("WECOM_CUSTOMER_SECRET")
    return {
        "enabled": cfg.enabled,
        "configured": cfg.enabled and not missing,
        "missing": missing,
        **callback,
        "corp_id": cfg.corp_id,
        "sync_enabled": cfg.sync_enabled,
        "dispatch_enabled": cfg.dispatch_enabled,
        "worker_interval_seconds": cfg.worker_interval_seconds,
        "sync_interval_seconds": cfg.sync_interval_seconds,
        "welcome_enabled": cfg.welcome_enabled,
        "callback_path": "/api/wecom-customer/callback",
        "send_capability": "official_group_message_confirmation",
    }


def _request_json(method: str, url: str, payload: dict | None = None, timeout: int = 15) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WecomCustomerApiError(f"客户联系接口请求失败：{exc}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WecomCustomerApiError("客户联系接口返回了非JSON响应") from exc
    errcode = int(result.get("errcode") or 0)
    if errcode:
        raise WecomCustomerApiError(f"客户联系接口错误 {errcode}：{result.get('errmsg', '')}", errcode)
    return result


def get_access_token(config: WecomCustomerConfig | None = None, force_refresh: bool = False) -> str:
    cfg = config or read_wecom_customer_config()
    key = (cfg.corp_id, cfg.secret)
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached and not force_refresh and cached[1] > now:
            return cached[0]
        query = urllib.parse.urlencode({"corpid": cfg.corp_id, "corpsecret": cfg.secret})
        result = _request_json("GET", f"{WECOM_API_BASE}/gettoken?{query}")
        token = str(result.get("access_token") or "").strip()
        if not token:
            raise WecomCustomerApiError("客户联系 access_token 为空")
        expires_in = max(int(result.get("expires_in") or 7200) - 300, 60)
        _TOKEN_CACHE[key] = (token, now + expires_in)
        return token


def _authorized_request(
    path: str,
    config: WecomCustomerConfig,
    *,
    method: str = "POST",
    payload: dict | None = None,
    query: dict | None = None,
) -> dict:
    def request(force_refresh: bool = False) -> dict:
        token = get_access_token(config, force_refresh=force_refresh)
        params = {"access_token": token, **(query or {})}
        url = f"{WECOM_API_BASE}/{path}?{urllib.parse.urlencode(params)}"
        return _request_json(method, url, payload)

    try:
        return request()
    except WecomCustomerApiError as exc:
        if exc.errcode not in {40014, 42001}:
            raise
    return request(force_refresh=True)


def get_follow_users(config: WecomCustomerConfig) -> list[str]:
    result = _authorized_request("externalcontact/get_follow_user_list", config, method="GET")
    return [str(item).strip() for item in result.get("follow_user") or [] if str(item).strip()]


def list_external_contacts(config: WecomCustomerConfig, userid: str) -> list[str]:
    result = _authorized_request(
        "externalcontact/list",
        config,
        method="GET",
        query={"userid": userid},
    )
    return [str(item).strip() for item in result.get("external_userid") or [] if str(item).strip()]


def get_external_contact(config: WecomCustomerConfig, external_userid: str) -> dict:
    cursor = ""
    contact: dict = {}
    follow_users: list[dict] = []
    while True:
        query = {"external_userid": external_userid}
        if cursor:
            query["cursor"] = cursor
        result = _authorized_request("externalcontact/get", config, method="GET", query=query)
        if isinstance(result.get("external_contact"), dict):
            contact = result["external_contact"]
        follow_users.extend(item for item in result.get("follow_user") or [] if isinstance(item, dict))
        cursor = str(result.get("next_cursor") or "").strip()
        if not cursor:
            break
    return {"external_contact": contact, "follow_user": follow_users}


def sync_customer_contacts(config: WecomCustomerConfig, userids: list[str] | None = None) -> dict:
    members = list(dict.fromkeys(item.strip() for item in (userids or get_follow_users(config)) if item.strip()))
    identities: dict[str, set[str]] = {}
    for userid in members:
        for external_userid in list_external_contacts(config, userid):
            identities.setdefault(external_userid, set()).add(userid)

    contacts: list[dict] = []
    for external_userid, expected_members in identities.items():
        detail = get_external_contact(config, external_userid)
        follow_users = [
            item
            for item in detail.get("follow_user") or []
            if str(item.get("userid") or "").strip() in expected_members
        ]
        contacts.append({**detail, "follow_user": follow_users})
    return {"members": members, "contacts": contacts}


def create_group_message(
    config: WecomCustomerConfig,
    *,
    sender: str,
    external_userids: list[str],
    content: str,
) -> dict:
    text = str(content or "").strip()
    if not text:
        raise WecomCustomerApiError("客户联系群发内容不能为空")
    if len(text.encode("utf-8")) > 4000:
        raise WecomCustomerApiError("客户联系群发文本不能超过4000字节")
    targets = list(dict.fromkeys(item.strip() for item in external_userids if item.strip()))
    if not sender.strip() or not targets:
        raise WecomCustomerApiError("客户联系群发缺少企业成员或客户ID")
    return _authorized_request(
        "externalcontact/add_msg_template",
        config,
        payload={
            "chat_type": "single",
            "external_userid": targets,
            "sender": sender.strip(),
            "allow_select": False,
            "text": {"content": text},
        },
    )


def send_welcome_message(config: WecomCustomerConfig, welcome_code: str, content: str) -> dict:
    text = str(content or "").strip()
    if not welcome_code.strip() or not text:
        raise WecomCustomerApiError("欢迎语缺少 welcome_code 或文本")
    if len(text.encode("utf-8")) > 4000:
        raise WecomCustomerApiError("客户联系欢迎语不能超过4000字节")
    return _authorized_request(
        "externalcontact/send_welcome_msg",
        config,
        payload={"welcome_code": welcome_code.strip(), "text": {"content": text}},
    )


def parse_customer_event(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)

    def value(name: str) -> str:
        node = root.find(name)
        return (node.text or "").strip() if node is not None else ""

    return {
        "to_user_name": value("ToUserName"),
        "create_time": value("CreateTime"),
        "msg_type": value("MsgType"),
        "event": value("Event"),
        "change_type": value("ChangeType"),
        "userid": value("UserID"),
        "external_userid": value("ExternalUserID"),
        "state": value("State"),
        "welcome_code": value("WelcomeCode"),
        "source": value("Source"),
    }


def decrypt_callback_request(
    body: str,
    *,
    signature: str,
    timestamp: str,
    nonce: str,
    config: WecomCustomerConfig,
) -> str:
    encrypted = xml_value(body, "Encrypt")
    if not encrypted:
        raise ValueError("客户联系回调缺少 Encrypt")
    if not verify_callback_signature(config.token, signature, timestamp, nonce, encrypted):
        raise ValueError("客户联系回调签名校验失败")
    return decrypt_callback_value(encrypted, config.encoding_aes_key, config.corp_id)


def verify_callback_echo(
    echo: str,
    *,
    signature: str,
    timestamp: str,
    nonce: str,
    config: WecomCustomerConfig,
) -> str:
    if not verify_callback_signature(config.token, signature, timestamp, nonce, echo):
        raise ValueError("客户联系回调签名校验失败")
    return decrypt_callback_value(echo, config.encoding_aes_key, config.corp_id)
