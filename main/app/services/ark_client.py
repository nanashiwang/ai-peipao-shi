"""豆包 Ark 调用封装。

这个模块只负责：读取本地私有配置文件、拼接请求、把模型返回内容尽量解析成 JSON。
"""

import json
import base64
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from openai import OpenAI


ROOT = Path(__file__).resolve().parents[2]
ARK_CONFIG = ROOT / "config" / "ark.json"
ARK_EXAMPLE_CONFIG = ROOT / "config" / "ark.example.json"
PLACEHOLDER_KEYS = {"", "your-api-key", "sk-your-api-key", "changeme", "change-me"}


# 环境变量缺失时抛出这个异常，方便上层回退到本地 mock 逻辑。
class ArkNotConfigured(RuntimeError):
    pass


@lru_cache
def ark_config() -> dict[str, str]:
    env_api_key = os.getenv("ARK_API_KEY", "").strip()
    env_endpoint_id = (os.getenv("ARK_ENDPOINT_ID", "") or os.getenv("ARK_MODEL_NAME", "")).strip()
    if env_api_key or env_endpoint_id:
        if env_api_key.lower() in PLACEHOLDER_KEYS or not env_endpoint_id:
            raise ArkNotConfigured("ARK_API_KEY and ARK_ENDPOINT_ID must be configured together")
        return {
            "base_url": os.getenv("ARK_BASE_URL", "").strip() or "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": env_api_key,
            "endpoint_id": env_endpoint_id,
            "model_name": os.getenv("ARK_MODEL_NAME", "").strip() or env_endpoint_id,
        }

    path = ARK_CONFIG if ARK_CONFIG.exists() else ARK_EXAMPLE_CONFIG
    if not path.exists():
        raise ArkNotConfigured("config/ark.json is not configured")
    data = json.loads(path.read_text(encoding="utf-8"))
    api_key = str(data.get("api_key", "")).strip()
    endpoint_id = str(data.get("endpoint_id", "")).strip()
    if api_key.lower() in PLACEHOLDER_KEYS or not endpoint_id:
        raise ArkNotConfigured("config/ark.json requires api_key and endpoint_id")
    return {
        "base_url": str(data.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3"),
        "api_key": api_key,
        "endpoint_id": endpoint_id,
        "model_name": str(data.get("model_name") or ""),
    }


# 只创建一次 OpenAI 客户端，减少重复初始化开销。
@lru_cache
def ark_client() -> OpenAI:
    config = ark_config()
    return OpenAI(base_url=config["base_url"], api_key=config["api_key"], timeout=60.0, max_retries=2)


# 读取 endpoint id。
def ark_endpoint_id() -> str:
    return ark_config()["endpoint_id"]


# 尽量从模型输出里提取 JSON 对象，兼容代码块和前后包裹文本。
def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise

# 统一封装 Ark 聊天接口调用，返回解析后的 JSON。
def call_ark_json(system_prompt: str, user_payload: dict[str, Any], temperature: float = 0.2) -> dict[str, Any]:
    response = ark_client().chat.completions.create(
        model=ark_endpoint_id(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        temperature=temperature,
    )
    content = response.choices[0].message.content or "{}"
    return extract_json_object(content)


def call_ark_vision_json(system_prompt: str, image_path: str | Path, user_text: str = "", temperature: float = 0.0) -> dict[str, Any]:
    path = Path(image_path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    response = ark_client().chat.completions.create(
        model=ark_endpoint_id(),
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text or "请识别图片中的文字，只输出 JSON。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            },
        ],
        temperature=temperature,
    )
    content = response.choices[0].message.content or "{}"
    return extract_json_object(content)
