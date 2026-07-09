"""企业微信会话内容存档接入。

该模块只负责官方 Finance SDK 的拉取/解密适配，以及把解密后的消息归一化成
后端现有会话同步结构；AI 生成和发送任务仍复用 app.main 里的通用链路。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


TEXT_MSG_TYPES = {"text", "markdown"}


@dataclass
class WecomArchiveConfig:
    enabled: bool
    corp_id: str
    secret: str
    private_key: str
    private_key_path: str
    sdk_path: str
    proxy: str = ""
    proxy_passwd: str = ""
    timeout: int = 30
    limit: int = 100
    self_userids: set[str] | None = None
    conversation_map: dict[str, Any] | None = None
    user_map: dict[str, str] | None = None
    auto_resolve_names: bool = True


@dataclass
class ArchiveEnvelope:
    seq: int
    msgid: str
    raw: dict[str, Any]
    decrypted: dict[str, Any]


@dataclass
class NormalizedArchiveMessage:
    target_name: str
    family_id: str
    speaker: str
    content: str
    message_time: datetime
    source: str
    external_id: str
    latest_inbound: bool = False
    archive_self_userids: list[str] | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_json(name: str) -> dict:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _env_set(name: str) -> set[str]:
    return {item.strip() for item in (os.getenv(name) or "").split(",") if item.strip()}


def read_wecom_archive_config() -> WecomArchiveConfig:
    return WecomArchiveConfig(
        enabled=_env_bool("WECOM_ARCHIVE_ENABLED", False),
        corp_id=(os.getenv("WECOM_ARCHIVE_CORP_ID") or "").strip(),
        secret=(os.getenv("WECOM_ARCHIVE_SECRET") or "").strip(),
        private_key=(os.getenv("WECOM_ARCHIVE_PRIVATE_KEY") or "").strip(),
        private_key_path=(os.getenv("WECOM_ARCHIVE_PRIVATE_KEY_PATH") or "").strip(),
        sdk_path=(os.getenv("WECOM_ARCHIVE_SDK_PATH") or "").strip(),
        proxy=(os.getenv("WECOM_ARCHIVE_PROXY") or "").strip(),
        proxy_passwd=(os.getenv("WECOM_ARCHIVE_PROXY_PASSWD") or "").strip(),
        timeout=_env_int("WECOM_ARCHIVE_TIMEOUT_SECONDS", 30),
        limit=_env_int("WECOM_ARCHIVE_LIMIT", 100),
        self_userids=_env_set("WECOM_ARCHIVE_SELF_USERIDS"),
        conversation_map=_env_json("WECOM_ARCHIVE_CONVERSATION_MAP"),
        user_map=_env_json("WECOM_ARCHIVE_USER_MAP"),
        auto_resolve_names=_env_bool("WECOM_ARCHIVE_AUTO_RESOLVE_NAMES", True),
    )


def config_status(config: WecomArchiveConfig | None = None) -> dict:
    cfg = config or read_wecom_archive_config()
    missing = []
    if not cfg.enabled:
        return {"enabled": False, "configured": False, "missing": [], "detail": "会话内容存档未启用"}
    for key, value in [
        ("WECOM_ARCHIVE_CORP_ID", cfg.corp_id),
        ("WECOM_ARCHIVE_SECRET", cfg.secret),
        ("WECOM_ARCHIVE_SDK_PATH", cfg.sdk_path),
    ]:
        if not value:
            missing.append(key)
    if not cfg.private_key and not cfg.private_key_path:
        missing.append("WECOM_ARCHIVE_PRIVATE_KEY 或 WECOM_ARCHIVE_PRIVATE_KEY_PATH")
    if cfg.sdk_path and not Path(cfg.sdk_path).exists():
        missing.append("WECOM_ARCHIVE_SDK_PATH 文件不存在")
    return {
        "enabled": cfg.enabled,
        "configured": not missing,
        "missing": missing,
        "corp_id": cfg.corp_id,
        "limit": cfg.limit,
        "self_userids": sorted(cfg.self_userids or []),
        "mapped_conversations": sorted((cfg.conversation_map or {}).keys()),
        "auto_resolve_names": cfg.auto_resolve_names,
        "detail": "会话内容存档已配置" if not missing else "会话内容存档配置不完整",
    }


class FinanceSdk:
    """企业微信会话内容存档 C SDK 的 ctypes 轻量封装。"""

    def __init__(self, config: WecomArchiveConfig):
        status = config_status(config)
        if not status["configured"]:
            raise RuntimeError(f"会话内容存档配置不完整：{', '.join(status['missing'])}")
        self.config = config
        self.lib = ctypes.cdll.LoadLibrary(config.sdk_path)
        self._bind()
        self.sdk = self.lib.NewSdk()
        ret = self.lib.Init(self.sdk, config.corp_id.encode("utf-8"), config.secret.encode("utf-8"))
        if ret != 0:
            raise RuntimeError(f"企业微信 Finance SDK 初始化失败：ret={ret}")

    def _bind(self) -> None:
        lib = self.lib
        lib.NewSdk.restype = ctypes.c_void_p
        lib.DestroySdk.argtypes = [ctypes.c_void_p]
        lib.DestroySdk.restype = None
        lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.Init.restype = ctypes.c_int
        lib.NewSlice.restype = ctypes.c_void_p
        lib.FreeSlice.argtypes = [ctypes.c_void_p]
        lib.FreeSlice.restype = None
        lib.GetContentFromSlice.argtypes = [ctypes.c_void_p]
        lib.GetContentFromSlice.restype = ctypes.c_char_p
        lib.GetChatData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulonglong,
            ctypes.c_uint,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        lib.GetChatData.restype = ctypes.c_int
        lib.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        lib.DecryptData.restype = ctypes.c_int

    def close(self) -> None:
        if getattr(self, "sdk", None):
            self.lib.DestroySdk(self.sdk)
            self.sdk = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _slice_text(self, slice_obj) -> str:
        data = self.lib.GetContentFromSlice(slice_obj)
        return data.decode("utf-8") if data else ""

    def get_chat_data(self, seq: int, limit: int) -> dict:
        out = self.lib.NewSlice()
        try:
            ret = self.lib.GetChatData(
                self.sdk,
                int(seq),
                int(limit),
                self.config.proxy.encode("utf-8"),
                self.config.proxy_passwd.encode("utf-8"),
                int(self.config.timeout),
                out,
            )
            if ret != 0:
                raise RuntimeError(f"GetChatData 失败：ret={ret}")
            return json.loads(self._slice_text(out) or "{}")
        finally:
            self.lib.FreeSlice(out)

    def decrypt_data(self, random_key: str, encrypt_chat_msg: str) -> dict:
        out = self.lib.NewSlice()
        try:
            ret = self.lib.DecryptData(random_key.encode("utf-8"), encrypt_chat_msg.encode("utf-8"), out)
            if ret != 0:
                raise RuntimeError(f"DecryptData 失败：ret={ret}")
            return json.loads(self._slice_text(out) or "{}")
        finally:
            self.lib.FreeSlice(out)


def _private_key_pem(config: WecomArchiveConfig) -> str:
    if config.private_key:
        text = config.private_key
        if "BEGIN" not in text:
            text = base64.b64decode(text).decode("utf-8")
        return text
    if config.private_key_path:
        return Path(config.private_key_path).read_text(encoding="utf-8")
    raise RuntimeError("未配置企业微信会话存档私钥")


def decrypt_random_key(encrypt_random_key: str, config: WecomArchiveConfig) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as exc:
        raise RuntimeError("缺少 cryptography，无法解密会话存档 random_key") from exc

    private_key = serialization.load_pem_private_key(_private_key_pem(config).encode("utf-8"), password=None)
    decrypted = private_key.decrypt(base64.b64decode(encrypt_random_key), padding.PKCS1v15())
    return decrypted.decode("utf-8")


def _archive_envelope_to_dict(envelope: ArchiveEnvelope) -> dict:
    return {
        "seq": envelope.seq,
        "msgid": envelope.msgid,
        "raw": envelope.raw,
        "decrypted": envelope.decrypted,
    }


def _archive_envelope_from_dict(data: dict[str, Any]) -> ArchiveEnvelope:
    return ArchiveEnvelope(
        seq=int(data.get("seq") or 0),
        msgid=str(data.get("msgid") or ""),
        raw=data.get("raw") if isinstance(data.get("raw"), dict) else {},
        decrypted=data.get("decrypted") if isinstance(data.get("decrypted"), dict) else {},
    )


def pull_archive_messages_direct(seq: int, limit: int | None = None, config: WecomArchiveConfig | None = None) -> list[ArchiveEnvelope]:
    cfg = config or read_wecom_archive_config()
    batch_limit = limit or cfg.limit
    envelopes: list[ArchiveEnvelope] = []
    with FinanceSdk(cfg) as sdk:
        data = sdk.get_chat_data(seq, batch_limit)
        for item in data.get("chatdata") or []:
            random_key = decrypt_random_key(str(item.get("encrypt_random_key") or ""), cfg)
            decrypted = sdk.decrypt_data(random_key, str(item.get("encrypt_chat_msg") or ""))
            envelopes.append(
                ArchiveEnvelope(
                    seq=int(item.get("seq") or 0),
                    msgid=str(item.get("msgid") or decrypted.get("msgid") or ""),
                    raw=item,
                    decrypted=decrypted,
                )
            )
    return envelopes


def pull_archive_messages_subprocess(seq: int, limit: int | None = None, config: WecomArchiveConfig | None = None) -> list[ArchiveEnvelope]:
    cfg = config or read_wecom_archive_config()
    batch_limit = limit or cfg.limit
    env = os.environ.copy()
    env["WECOM_ARCHIVE_SDK_SUBPROCESS"] = "false"
    command = [
        sys.executable,
        "-m",
        "app.services.wecom_archive",
        "--pull-archive-json",
        str(int(seq)),
        str(int(batch_limit)),
    ]
    project_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        command,
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=max(int(cfg.timeout or 30) + 30, 60),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"企业微信 Finance SDK 子进程失败：exit={completed.returncode} {detail[:500]}")
    try:
        data = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"企业微信 Finance SDK 子进程输出不是 JSON：{completed.stdout[:500]}") from exc
    return [_archive_envelope_from_dict(item) for item in data if isinstance(item, dict)]


def pull_archive_messages(seq: int, limit: int | None = None, config: WecomArchiveConfig | None = None) -> list[ArchiveEnvelope]:
    if _env_bool("WECOM_ARCHIVE_SDK_SUBPROCESS", True):
        return pull_archive_messages_subprocess(seq, limit, config)
    return pull_archive_messages_direct(seq, limit, config)


def _pull_archive_json_cli(argv: list[str]) -> int:
    seq = int(argv[0]) if argv else 0
    limit = int(argv[1]) if len(argv) > 1 else None
    envelopes = pull_archive_messages_direct(seq, limit)
    print(json.dumps([_archive_envelope_to_dict(item) for item in envelopes], ensure_ascii=False))
    return 0


def parse_archive_time(value) -> datetime:
    try:
        number = int(value)
    except Exception:
        return datetime.utcnow()
    if number > 10_000_000_000:
        number = number // 1000
    return datetime.fromtimestamp(number)


def archive_message_content(message: dict[str, Any]) -> str:
    msgtype = str(message.get("msgtype") or "").strip()
    if msgtype == "text":
        return str((message.get("text") or {}).get("content") or "").strip()
    if msgtype == "markdown":
        return str((message.get("markdown") or {}).get("content") or "").strip()
    return ""


def _map_lookup(config: WecomArchiveConfig, *keys: str) -> dict:
    mapping = config.conversation_map or {}
    for key in keys:
        if not key:
            continue
        value = mapping.get(key)
        if isinstance(value, str):
            return {"target_name": value}
        if isinstance(value, dict):
            return value
    return {}


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_USER_NAME_CACHE: dict[str, str] = {}
_ROOM_NAME_CACHE: dict[str, str] = {}


def _wecom_api_token(config: WecomArchiveConfig) -> str:
    key = f"{config.corp_id}:{hashlib.sha1(config.secret.encode('utf-8')).hexdigest()[:8]}"
    cached = _TOKEN_CACHE.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    params = urllib.parse.urlencode({"corpid": config.corp_id, "corpsecret": config.secret})
    with urllib.request.urlopen(f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?{params}", timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("errcode") != 0:
        return ""
    token = str(data.get("access_token") or "")
    if token:
        _TOKEN_CACHE[key] = (token, time.time() + int(data.get("expires_in") or 7200))
    return token


def _wecom_api_get(path: str, token: str, params: dict[str, Any] | None = None) -> dict:
    query = {"access_token": token, **(params or {})}
    url = f"https://qyapi.weixin.qq.com/cgi-bin{path}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _wecom_api_post(path: str, token: str, payload: dict[str, Any]) -> dict:
    url = f"https://qyapi.weixin.qq.com/cgi-bin{path}?access_token={urllib.parse.quote(token)}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _auto_resolve_user_name(userid: str, config: WecomArchiveConfig) -> str:
    clean = (userid or "").strip()
    if not clean:
        return ""
    mapped = str((config.user_map or {}).get(clean) or "").strip()
    if mapped:
        return mapped
    if clean in _USER_NAME_CACHE:
        return _USER_NAME_CACHE[clean]
    if not config.auto_resolve_names:
        return clean
    try:
        token = _wecom_api_token(config)
        data = _wecom_api_get("/user/get", token, {"userid": clean}) if token else {}
        name = str(data.get("name") or data.get("alias") or clean).strip() if data.get("errcode") == 0 else clean
    except Exception:
        name = clean
    _USER_NAME_CACHE[clean] = name
    return name


def _auto_resolve_room_name(roomid: str, config: WecomArchiveConfig) -> str:
    clean = (roomid or "").strip()
    if not clean:
        return ""
    if clean in _ROOM_NAME_CACHE:
        return _ROOM_NAME_CACHE[clean]
    if not config.auto_resolve_names:
        return clean
    try:
        token = _wecom_api_token(config)
        data = _wecom_api_post("/msgaudit/groupchat/get", token, {"roomid": clean}) if token else {}
        name = str(data.get("roomname") or clean).strip() if data.get("errcode") == 0 else clean
    except Exception:
        name = clean
    _ROOM_NAME_CACHE[clean] = name
    return name


def _display_user(userid: str, config: WecomArchiveConfig) -> str:
    return _auto_resolve_user_name(userid, config)


def _conversation_key(message: dict[str, Any]) -> str:
    roomid = str(message.get("roomid") or "").strip()
    if roomid:
        return roomid
    users = [str(message.get("from") or "").strip(), *[str(x).strip() for x in (message.get("tolist") or [])]]
    return "|".join(sorted(x for x in users if x))


def _archive_self_userids_for_message(message: dict[str, Any], config: WecomArchiveConfig) -> list[str]:
    self_userids = config.self_userids or set()
    if not self_userids:
        return []
    sender = str(message.get("from") or "").strip()
    tolist = [str(item).strip() for item in (message.get("tolist") or []) if str(item).strip()]
    participants = {item for item in [sender, *tolist] if item}
    matched = sorted(item for item in self_userids if item in participants)
    if matched:
        return matched
    # 单账号部署下部分群聊存档不会在 tolist 里展开成员；此时可安全归属到唯一账号。
    return sorted(self_userids) if len(self_userids) == 1 else []


def _private_chat_target_name(participants: list[str], sender: str, config: WecomArchiveConfig, conv_key: str) -> str:
    self_userids = config.self_userids or set()
    others = [item for item in participants if item and item not in self_userids]
    if len(others) == 1:
        return _display_user(others[0], config)
    if self_userids and others:
        return " / ".join(_display_user(item, config) for item in others)
    if len(participants) == 1:
        return _display_user(participants[0], config)
    return " / ".join(_display_user(item, config) for item in participants) or conv_key


def _archive_family_id(roomid: str, conv_key: str, target_name: str, mapping: dict) -> str:
    if mapping.get("family_id"):
        return str(mapping["family_id"]).strip()[:64]
    if mapping.get("target_name") and target_name:
        return f"WECOM_{target_name}"[:64]
    if roomid:
        return f"WECOM_{roomid}"[:64]
    digest = hashlib.sha1(conv_key.encode("utf-8")).hexdigest()[:16]
    return f"WECOM_DM_{digest}"


def normalize_archive_message(envelope: ArchiveEnvelope, config: WecomArchiveConfig | None = None) -> NormalizedArchiveMessage | None:
    cfg = config or read_wecom_archive_config()
    message = envelope.decrypted
    content = archive_message_content(message)
    if not content:
        return None

    sender = str(message.get("from") or "").strip()
    tolist = [str(item).strip() for item in (message.get("tolist") or []) if str(item).strip()]
    participants = sorted({item for item in [sender, *tolist] if item})
    roomid = str(message.get("roomid") or "").strip()
    conv_key = _conversation_key(message)
    mapping = _map_lookup(cfg, roomid, conv_key, sender, *(tolist or []))
    self_userids = cfg.self_userids or set()
    is_self = bool(sender and sender in self_userids)
    speaker = "我" if is_self else _display_user(sender, cfg)
    archive_self_userids = _archive_self_userids_for_message(message, cfg)

    if mapping.get("target_name"):
        target_name = str(mapping["target_name"]).strip()
    elif roomid:
        target_name = _auto_resolve_room_name(roomid, cfg)
    else:
        target_name = _private_chat_target_name(participants, sender, cfg, conv_key)
    family_id = _archive_family_id(roomid, conv_key, target_name, mapping)

    msgid = envelope.msgid or str(message.get("msgid") or "")
    return NormalizedArchiveMessage(
        target_name=target_name,
        family_id=family_id,
        speaker=speaker,
        content=content,
        message_time=parse_archive_time(message.get("msgtime")),
        source=f"企业微信存档:{message.get('msgtype', 'unknown')}",
        external_id=f"wecom_archive:{msgid}" if msgid else "",
        latest_inbound=not is_self,
        archive_self_userids=archive_self_userids,
    )


def group_archive_messages(messages: list[NormalizedArchiveMessage]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for msg in messages:
        key = (msg.family_id, msg.target_name)
        item = grouped.setdefault(
            key,
            {
                "family_id": msg.family_id,
                "target_name": msg.target_name,
                "messages": [],
                "latest_message": "",
                "archive_self_userids": [],
            },
        )
        item["messages"].append(msg)
        if msg.latest_inbound:
            item["latest_message"] = msg.content
            item["archive_self_userids"] = sorted(set(item["archive_self_userids"]) | set(msg.archive_self_userids or []))
    return list(grouped.values())


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--pull-archive-json":
        raise SystemExit(_pull_archive_json_cli(sys.argv[2:]))
    raise SystemExit("unsupported command")
