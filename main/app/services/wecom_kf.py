"""微信客服 API 的配置、回调解密和消息收发封装。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
SUPPORTED_INBOUND_TYPES = {"text", "image", "voice", "video", "file", "location", "merged_msg", "miniprogram"}
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


class WecomKfApiError(RuntimeError):
    def __init__(self, message: str, errcode: int = -1):
        super().__init__(message)
        self.errcode = errcode


@dataclass(frozen=True)
class WecomKfConfig:
    enabled: bool
    corp_id: str
    secret: str
    token: str
    encoding_aes_key: str
    default_open_kfid: str = ""
    poll_enabled: bool = True
    poll_interval_seconds: int = 5
    dispatch_enabled: bool = True


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


def read_wecom_kf_config() -> WecomKfConfig:
    return WecomKfConfig(
        enabled=_env_bool("WECOM_KF_ENABLED", False),
        corp_id=os.getenv("WECOM_KF_CORP_ID", "").strip(),
        secret=os.getenv("WECOM_KF_SECRET", "").strip(),
        token=os.getenv("WECOM_KF_TOKEN", "").strip(),
        encoding_aes_key=os.getenv("WECOM_KF_ENCODING_AES_KEY", "").strip(),
        default_open_kfid=os.getenv("WECOM_KF_DEFAULT_OPEN_KFID", "").strip(),
        poll_enabled=_env_bool("WECOM_KF_POLL_ENABLED", True),
        poll_interval_seconds=_env_int("WECOM_KF_POLL_INTERVAL_SECONDS", 5, 2, 300),
        dispatch_enabled=_env_bool("WECOM_KF_DISPATCH_ENABLED", True),
    )


def config_status(config: WecomKfConfig | None = None) -> dict:
    cfg = config or read_wecom_kf_config()
    required = {
        "WECOM_KF_CORP_ID": cfg.corp_id,
        "WECOM_KF_SECRET": cfg.secret,
        "WECOM_KF_TOKEN": cfg.token,
        "WECOM_KF_ENCODING_AES_KEY": cfg.encoding_aes_key,
    }
    missing = [name for name, value in required.items() if not value]
    if cfg.encoding_aes_key and len(cfg.encoding_aes_key) != 43:
        missing.append("WECOM_KF_ENCODING_AES_KEY(必须43位)")
    return {
        "enabled": cfg.enabled,
        "configured": cfg.enabled and not missing,
        "missing": missing,
        "corp_id": cfg.corp_id,
        "default_open_kfid": cfg.default_open_kfid,
        "poll_enabled": cfg.poll_enabled,
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "dispatch_enabled": cfg.dispatch_enabled,
        "callback_path": "/api/wecom-kf/callback",
        "prompt_source": "Agent配置 / ai_reply_agent",
        "knowledge_source": "Agent配置 / 知识库",
    }


def callback_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    raw = "".join(sorted([str(token), str(timestamp), str(nonce), str(encrypted)]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verify_callback_signature(token: str, signature: str, timestamp: str, nonce: str, encrypted: str) -> bool:
    expected = callback_signature(token, timestamp, nonce, encrypted)
    return bool(signature) and hmac.compare_digest(expected, signature)


def _aes_key(encoding_aes_key: str) -> bytes:
    if len(encoding_aes_key or "") != 43:
        raise ValueError("WECOM_KF_ENCODING_AES_KEY 必须为43位")
    return base64.b64decode(f"{encoding_aes_key}=")


def decrypt_callback_value(encrypted: str, encoding_aes_key: str, receive_id: str = "") -> str:
    key = _aes_key(encoding_aes_key)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
    unpadder = padding.PKCS7(256).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    if len(plain) < 20:
        raise ValueError("微信客服回调密文长度不合法")
    msg_len = struct.unpack(">I", plain[16:20])[0]
    message = plain[20 : 20 + msg_len]
    actual_receive_id = plain[20 + msg_len :].decode("utf-8")
    if receive_id and actual_receive_id and actual_receive_id != receive_id:
        raise ValueError("微信客服回调 receive_id 不匹配")
    return message.decode("utf-8")


def xml_value(xml_text: str, name: str) -> str:
    root = ET.fromstring(xml_text)
    node = root.find(name)
    return (node.text or "").strip() if node is not None else ""


def parse_callback_event(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)

    def value(name: str) -> str:
        node = root.find(name)
        return (node.text or "").strip() if node is not None else ""

    return {
        "to_user_name": value("ToUserName"),
        "create_time": value("CreateTime"),
        "msg_type": value("MsgType"),
        "event": value("Event"),
        "token": value("Token"),
        "open_kfid": value("OpenKfId"),
    }


def decrypt_callback_request(
    body: str,
    *,
    signature: str,
    timestamp: str,
    nonce: str,
    config: WecomKfConfig,
) -> str:
    encrypted = xml_value(body, "Encrypt")
    if not encrypted:
        raise ValueError("微信客服回调缺少 Encrypt")
    if not verify_callback_signature(config.token, signature, timestamp, nonce, encrypted):
        raise ValueError("微信客服回调签名校验失败")
    return decrypt_callback_value(encrypted, config.encoding_aes_key, config.corp_id)


def verify_callback_echo(
    echo: str,
    *,
    signature: str,
    timestamp: str,
    nonce: str,
    config: WecomKfConfig,
) -> str:
    if not verify_callback_signature(config.token, signature, timestamp, nonce, echo):
        raise ValueError("微信客服回调签名校验失败")
    return decrypt_callback_value(echo, config.encoding_aes_key, config.corp_id)


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
        raise WecomKfApiError(f"微信客服接口请求失败：{exc}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WecomKfApiError("微信客服接口返回了非JSON响应") from exc
    errcode = int(result.get("errcode") or 0)
    if errcode:
        raise WecomKfApiError(f"微信客服接口错误 {errcode}：{result.get('errmsg', '')}", errcode)
    return result


def get_access_token(config: WecomKfConfig | None = None, force_refresh: bool = False) -> str:
    cfg = config or read_wecom_kf_config()
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
            raise WecomKfApiError("微信客服 access_token 为空")
        expires_in = max(int(result.get("expires_in") or 7200) - 300, 60)
        _TOKEN_CACHE[key] = (token, now + expires_in)
        return token


def _authorized_request(path: str, payload: dict, config: WecomKfConfig) -> dict:
    token = get_access_token(config)
    url = f"{WECOM_API_BASE}/{path}?access_token={urllib.parse.quote(token)}"
    try:
        return _request_json("POST", url, payload)
    except WecomKfApiError as exc:
        if exc.errcode not in {40014, 42001}:
            raise
    token = get_access_token(config, force_refresh=True)
    url = f"{WECOM_API_BASE}/{path}?access_token={urllib.parse.quote(token)}"
    return _request_json("POST", url, payload)


def sync_messages(
    config: WecomKfConfig,
    *,
    cursor: str = "",
    event_token: str = "",
    open_kfid: str = "",
    limit: int = 1000,
) -> dict:
    payload: dict = {"limit": min(max(int(limit), 1), 1000), "voice_format": 0}
    if cursor:
        payload["cursor"] = cursor
    if event_token:
        payload["token"] = event_token
    if open_kfid:
        payload["open_kfid"] = open_kfid
    return _authorized_request("kf/sync_msg", payload, config)


def batch_get_customers(config: WecomKfConfig, external_userids: list[str]) -> dict[str, dict]:
    ids = list(dict.fromkeys(item for item in external_userids if item))[:1000]
    if not ids:
        return {}
    customers: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        result = _authorized_request("kf/customer/batchget", {"external_userid_list": ids[start : start + 100]}, config)
        customers.update({
            str(item.get("external_userid") or ""): item
            for item in result.get("customer_list") or []
            if item.get("external_userid")
        })
    return customers


def send_text(
    config: WecomKfConfig,
    *,
    external_userid: str,
    open_kfid: str,
    content: str,
    msgid: str,
) -> dict:
    if len(content.encode("utf-8")) > 2048:
        raise WecomKfApiError("微信客服文本消息不能超过2048字节")
    payload = {
        "touser": external_userid,
        "open_kfid": open_kfid,
        "msgid": msgid[:32],
        "msgtype": "text",
        "text": {"content": content},
    }
    return _authorized_request("kf/send_msg", payload, config)


def stable_family_id(external_userid: str) -> str:
    digest = hashlib.sha256(external_userid.encode("utf-8")).hexdigest()[:20]
    return f"WECOM_KF_{digest}"


def normalized_inbound_message(message: dict, customer: dict | None = None) -> dict | None:
    if int(message.get("origin") or 0) != 3:
        return None
    msgtype = str(message.get("msgtype") or "").strip()
    if msgtype not in SUPPORTED_INBOUND_TYPES:
        return None
    external_userid = str(message.get("external_userid") or "").strip()
    open_kfid = str(message.get("open_kfid") or "").strip()
    msgid = str(message.get("msgid") or "").strip()
    if not external_userid or not open_kfid or not msgid:
        return None
    content = ""
    auto_reply = msgtype == "text"
    if msgtype == "text":
        content = str((message.get("text") or {}).get("content") or "").strip()
    elif msgtype == "location":
        location = message.get("location") or {}
        content = f"[位置] {location.get('name') or ''} {location.get('address') or ''}".strip()
    else:
        labels = {
            "image": "图片",
            "voice": "语音",
            "video": "视频",
            "file": "文件",
            "merged_msg": "聊天记录",
            "miniprogram": "小程序",
        }
        content = f"[{labels.get(msgtype, msgtype)}]"
    if not content:
        return None
    customer = customer or {}
    display_name = str(customer.get("nickname") or "").strip() or f"微信客户-{external_userid[-6:]}"
    try:
        send_time = int(message.get("send_time") or 0)
        if send_time <= 0:
            raise ValueError("missing send_time")
        message_time = datetime.utcfromtimestamp(send_time).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        message_time = datetime.utcnow().isoformat()
    return {
        "family_id": stable_family_id(external_userid),
        "target_name": display_name,
        "display_name": display_name,
        "external_userid": external_userid,
        "open_kfid": open_kfid,
        "msgid": msgid,
        "message_time": message_time,
        "content": content,
        "msgtype": msgtype,
        "auto_reply": auto_reply,
    }
