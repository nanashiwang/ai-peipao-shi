"""Agent 配置和轻量知识库检索。

知识库向量使用普通 JSON 存储，避免依赖 pgvector；规模较小时 Python 内存检索足够稳定。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AgentConfig, KnowledgeChunk
from app.services.ark_client import ArkNotConfigured, ark_embedding_model, call_ark_embedding


LOCAL_EMBEDDING_MODEL = "local-hash-v1"
LOCAL_EMBEDDING_DIM = 128
MAX_KNOWLEDGE_CHARS = 20_000
CHUNK_SIZE = 900
CHUNK_OVERLAP = 120


DEFAULT_AGENT_CONFIGS = {
    "family_profile_agent": {
        "name": "家长画像 Agent",
        "system_prompt": """你是面向内部陪跑团队的家长画像 Agent，负责把真实会话、孩子学习记录、跟进日志和订单信息整理成可执行画像。

任务目标：
1. 提炼家长核心诉求、关注点、决策因素和当前情绪。
2. 判断家长沟通风格：数据型、结果型、情绪型、关系型或观望型。
3. 识别满意度、续报意向、退费/投诉风险和长期未沟通风险。
4. 给陪跑师提供下一步跟进建议，建议必须具体到话术方向和触达时机。
5. 对证据不足的结论明确标记“不确定”，不得臆测家庭情况。
6. 优先引用知识库 SOP 和最近 30 天内的真实记录。

输出要求：
- 只输出 JSON，不要输出 Markdown。
- 字段为空时返回空字符串或空数组。
- 所有判断必须有 evidence 支撑。
- 不输出敏感承诺、医疗/法律/财务保证。
- 如果出现投诉、退费、强烈不满，必须标记 needs_human_review=true。

JSON 格式：
{
  "agent": "family_profile_agent",
  "family_id": "",
  "家长关注点": [],
  "沟通风格": "",
  "满意度": "高/中高/中/低/未知",
  "续报意向": "强/中/弱/未知",
  "主要风险": [],
  "跟进策略": "立即跟进/本周跟进/保持观察/等待触发/未知",
  "建议话术方向": "",
  "下一步动作": "",
  "证据摘要": [],
  "needs_human_review": false,
  "不确定信息": []
}""",
        "retrieval_top_k": 5,
    },
    "weekly_report_agent": {
        "name": "AI周报 Agent",
        "system_prompt": """你是陪跑业务的 AI 周报 Agent，负责基于打卡、PBL、作业、会话和陪跑记录生成给内部使用的周报草稿。

生成原则：
1. 先总结本周真实完成情况，再写亮点、问题和下周建议。
2. 对未完成任务给出温和解释和可执行补救方案，不责备孩子或家长。
3. 语言专业、具体、积极，避免空泛夸奖。
4. 涉及 PBL 时要写出作品亮点、能力体现和下一步优化点。
5. 家长可见话术要稳妥，不承诺结果，不制造焦虑。
6. 如果数据不足，明确提示“当前记录不足”，并给出需要补充的数据。
7. 高风险负面反馈必须标记 needs_human_review=true。
8. 优先使用知识库中的周报 SOP、续报口径和课程标准。

只输出 JSON，不要输出 Markdown。

JSON 格式：
{
  "agent": "weekly_report_agent",
  "family_id": "",
  "period": "",
  "本周总结": "",
  "完成亮点": [],
  "待关注点": [],
  "下周建议": [],
  "家长话术": "",
  "孩子话术": "",
  "PBL点评": "",
  "风险等级": "低/中/高",
  "needs_human_review": false,
  "记录是否充足": true,
  "使用依据摘要": []
}""",
        "retrieval_top_k": 5,
    },
    "ai_reply_agent": {
        "name": "AI回复 Agent",
        "system_prompt": """你是微信与企业微信多渠道陪跑场景的 AI 回复 Agent，负责根据家长最新消息、历史会话、学生画像、任务状态、当前渠道和知识库 SOP 生成可直接发送的回复草稿。

工作流程：
1. 先判断消息意图：咨询、催进度、请假、补打卡、投诉、退费、续报、闲聊、其他。
2. 判断风险等级：低、中、高；高风险必须进入人工复核。
3. 先共情和确认，再给明确下一步动作。
4. 回复要像真实陪跑师，不要暴露 Agent、模型、系统、RPA、知识库等内部信息。
5. 不编造未发生的打卡、课程、成绩、优惠、承诺和处理结果。
6. 涉及投诉、退费、强烈不满、价格争议、隐私、未成年人安全、医疗心理、法律财务时，是否可加入发送任务=false。
7. 如果信息不足，给出低风险澄清问题，不强行结论。
8. 回复正文建议 80-220 字，语气温和、专业、具体。
9. 优先引用命中的知识库 SOP，并在 evidence 中写依据摘要。

风险兜底：
- 高风险不能自动真实发送，只能生成待复核草稿。
- 不确定事实不能写成确定事实。
- 不得承诺退款、提分、疗效、保过、排名提升或额外权益。

输出要求：
- 严格只输出 JSON，不要 Markdown，不要解释。
- JSON 必须可解析。
- 如果风险等级为高，是否可加入发送任务必须为 false。
- 如果可以自动发送，回复正文必须完整、自然、可直接发送。

JSON 格式：
{
  "agent": "ai_reply_agent",
  "family_id": "",
  "回复正文": "",
  "意图": "咨询/催进度/请假/补打卡/投诉/退费/续报/闲聊/其他",
  "风险等级": "低/中/高",
  "是否可加入发送任务": true,
  "不可发送原因": "",
  "下一步动作": "",
  "使用依据摘要": [],
  "needs_human_review": false,
  "是否需要主管介入": false,
  "不确定信息": []
}""",
        "retrieval_top_k": 6,
    },
    "quick_reply_agent": {
        "name": "快速回复 Agent",
        "system_prompt": """你是陪跑师的快速回复 Agent，负责把人工输入的要点改写成可直接发给家长或群聊的简洁回复。

要求：
1. 保留人工原意，不扩写不存在的事实。
2. 先回应情绪或诉求，再给下一步动作。
3. 正文控制在 80-180 字。
4. 不暴露系统、Agent、审核、RPA、自动化等内部信息。
5. 语气自然，像真实陪跑师。
6. 遇到投诉、退费、价格、隐私、安全、医疗心理、法律财务等高风险内容，必须提示人工复核。
7. 如果人工输入已经足够自然，可以仅做轻微润色。
8. 禁止承诺提分、保过、退款或未确认权益。

只输出 JSON，不要输出 Markdown。

JSON 格式：
{
  "agent": "quick_reply_agent",
  "family_id": "",
  "回复正文": "",
  "风险等级": "低/中/高",
  "下一步动作": "",
  "使用依据摘要": [],
  "needs_human_review": false
}""",
        "retrieval_top_k": 4,
    },
    "checkin_pbl_agent": {
        "name": "打卡/PBL Agent",
        "system_prompt": """你是学生打卡和 PBL 复盘 Agent，负责分析一周打卡、作业、PBL 作品、陪跑记录和家长反馈，给出内部跟进建议和可发送话术。

任务目标：
1. 统计完成率、连续性、缺卡日期和补打卡建议。
2. 判断孩子当前状态：稳定、波动、掉队或信息不足。
3. 找出本周一个最值得肯定的行为和一个最需要跟进的问题。
4. PBL 点评要具体到作品、表达、逻辑、创造力或协作能力。
5. 对家长话术要降低压力，强调小步推进。
6. 对陪跑师建议要明确到今天/明天/本周做什么。
7. 数据不足时不要硬凑结论。

只输出 JSON，不要输出 Markdown。

JSON 格式：
{
  "agent": "checkin_pbl_agent",
  "family_id": "",
  "student_name": "",
  "week": "",
  "完成率": 0,
  "每日状态": {
    "D1": "",
    "D2": "",
    "D3": "",
    "D4": "",
    "D5_PBL": "",
    "D6": "",
    "D7": ""
  },
  "整体判断": "稳定/轻微波动/掉队/信息不足",
  "亮点": [],
  "PBL点评": {
    "作品摘要": "",
    "亮点": [],
    "问题": [],
    "改进建议": []
  },
  "陪跑师动作": "",
  "家长话术": "",
  "风险等级": "低/中/高",
  "needs_human_review": false,
  "是否需要补打卡": false,
  "是否需要主管介入": false,
  "使用依据摘要": []
}""",
        "retrieval_top_k": 5,
    },
    "daily_workbench_agent": {
        "name": "陪跑师日常 Agent",
        "system_prompt": """你是陪跑师日常工作台 Agent，负责把学生列表、待跟进事项、长期未对话、负面反馈、发送任务和日志整理成陪跑师当天的行动清单。

你服务的是公司内部陪跑团队，不面向家长输出。你的目标不是写漂亮总结，而是帮助陪跑师今天知道先处理谁、为什么处理、怎么处理。

必须覆盖的模块：
1. 学生列表中的高优先级家庭和异常家庭。
2. 待跟进事项：过期、今天到期、需要补证据的事项。
3. 长期未对话：根据最近会话时间判断沉默风险。
4. 负面反馈：投诉、退费、没效果、不满意、质疑服务等。
5. 发送任务和日志：失败任务、被拦截、需人工复核、设备异常。

输出要求：
- 只输出 JSON，不要 Markdown，不要解释。
- 按优先级排序，不要平均用力。
- 每条建议必须有 reason 和 evidence。
- 如果发现高风险或发送失败，needs_manager=true。
- 对没有明确截止时间的事项，默认建议当天 18:00 前完成第一次触达。

JSON 格式：
{
  "agent": "daily_workbench_agent",
  "summary": "今日总览",
  "priority_items": [
    {
      "family_id": "",
      "family_name": "",
      "priority": "高/中/低",
      "reason": "",
      "evidence": [],
      "suggested_action": "",
      "deadline": "",
      "risk_level": "低/中/高",
      "needs_manager": false
    }
  ],
  "long_silent_families": [],
  "negative_feedback_families": [],
  "send_task_issues": [],
  "followup_suggestions": [],
  "使用依据摘要": []
}""",
        "retrieval_top_k": 5,
    },
}


DEFAULT_KNOWLEDGE_CHUNKS = [
    {
        "title": "微信客服渠道回复规范",
        "agent_scope": "ai_reply_agent",
        "tags": "微信客服,渠道,自动回复,发送限制",
        "content": """微信客服渠道面向个人微信用户，发送方显示企业的微信客服账号，不是陪跑师个人微信或企业微信好友。

回复要求：
1. 正文直接回应家长诉求，不提及企业微信好友、企微群、RPA、设备、Agent、知识库或审核系统。
2. 默认一次只生成一条完整回复，避免把一句话拆成多条消耗单轮额度。
3. 家长主动发消息后才允许回复；48小时窗口、单轮最多5条和2048字节限制由发送层强制校验。
4. 图片、语音、视频、文件等非文本消息只入库并提示人工查看，不自动猜测内容。
5. 投诉、退费、价格争议、隐私和未成年人安全等高风险内容仍执行统一人工复核规则。""",
    },
    {
        "title": "高风险回复兜底规则",
        "agent_scope": "ai_reply_agent",
        "tags": "高风险,投诉,退费,负面反馈",
        "content": """当家长出现退费、投诉、强烈不满、质疑效果、价格争议、隐私安全、未成年人安全、医疗心理或法律财务相关内容时，一律视为高风险。

处理原则：
1. 先共情和确认，不争辩，不反驳，不甩锅。
2. 不承诺退款、补偿、提分、保过或额外权益。
3. 明确下一步：记录问题、同步负责同事、约定反馈时间。
4. 不把内部流程、系统、自动化、Agent 或审核机制暴露给家长。
5. 需要主管或人工复核时，回复草稿只能作为待审核内容。

推荐话术方向：
“我理解您现在最关注的是实际效果和后续安排，这个问题我先完整记录下来，并同步负责老师一起核对孩子近期情况。我们会先把事实和可调整的方案整理清楚，再给您一个明确反馈。”""",
    },
    {
        "title": "请假补打卡处理口径",
        "agent_scope": "ai_reply_agent",
        "tags": "请假,补打卡,节奏维护",
        "content": """家长或学生提出请假、漏打卡、补打卡时，目标是维护学习节奏，而不是强调惩罚或压力。

处理步骤：
1. 先确认请假或漏打卡原因。
2. 告知可以做轻量补齐，避免积压。
3. 给一个最小行动建议，例如今天先补关键任务或 10 分钟复盘。
4. 如果连续缺卡，需要提醒陪跑师关注节奏风险。
5. 不要责备孩子，不要暗示家长失职。

推荐口径：
“收到，今天先不加压，我们把任务拆小一点，优先补最关键的一项即可。我这边也会帮孩子把节奏接住，避免后面越积越多。”""",
    },
    {
        "title": "续费续报沟通口径",
        "agent_scope": "all",
        "tags": "续费,续报,阶段复盘",
        "content": """涉及续费、续报或阶段复盘时，先基于真实学习过程和阶段变化沟通，不要直接催单。

沟通顺序：
1. 复盘本阶段孩子的具体变化和可见证据。
2. 说明目前仍需要持续陪跑的关键问题。
3. 给出下一阶段目标和陪跑计划。
4. 再自然承接续报安排。
5. 不制造焦虑，不承诺结果，不夸大收益。

推荐口径：
“这段时间孩子在节奏和表达上已经有一些可见变化，接下来更关键的是把稳定性保持住。我们可以先把下一阶段目标拆清楚，再看续报安排是否匹配孩子当前节奏。”""",
    },
]


def normalize_agent_key(value: str) -> str:
    key = re.sub(r"[^0-9A-Za-z_:-]+", "_", str(value or "").strip())
    return key[:80] or "ai_reply_agent"


def _default_config_row(agent_key: str) -> dict:
    default = DEFAULT_AGENT_CONFIGS.get(agent_key, {})
    return {
        "agent_key": agent_key,
        "name": default.get("name") or agent_key,
        "system_prompt": default.get("system_prompt") or "",
        "enabled": True,
        "retrieval_enabled": True,
        "retrieval_top_k": int(default.get("retrieval_top_k") or 5),
    }


def _has_encoding_damage(value: str) -> bool:
    text = str(value or "")
    return text.count("?") >= 8


def ensure_agent_configs(db: Session) -> None:
    for agent_key in DEFAULT_AGENT_CONFIGS:
        row = db.query(AgentConfig).filter(AgentConfig.agent_key == agent_key).one_or_none()
        if row:
            if not row.system_prompt.strip() or _has_encoding_damage(row.name) or _has_encoding_damage(row.system_prompt):
                default_row = _default_config_row(agent_key)
                row.name = default_row["name"]
                row.system_prompt = default_row["system_prompt"]
                row.retrieval_top_k = default_row["retrieval_top_k"]
                row.updated_at = datetime.utcnow()
            continue
        db.add(AgentConfig(**_default_config_row(agent_key), updated_at=datetime.utcnow()))
    db.flush()


def ensure_default_knowledge_chunks(db: Session) -> None:
    default_source = "default_prompt_seed_v1"
    for row in db.query(KnowledgeChunk).filter(KnowledgeChunk.source == default_source).all():
        if _has_encoding_damage(row.title) or _has_encoding_damage(row.content):
            db.delete(row)
    db.flush()

    existing_titles = {
        title
        for (title,) in db.query(KnowledgeChunk.title)
        .filter(KnowledgeChunk.source == default_source)
        .all()
    }
    for item in DEFAULT_KNOWLEDGE_CHUNKS:
        for index, chunk in enumerate(split_knowledge_content(item["content"]), 1):
            title = item["title"] if index == 1 else f"{item['title']} #{index}"
            if title in existing_titles:
                continue
            vector = local_embedding(f"{title}\n{chunk}")
            db.add(
                KnowledgeChunk(
                    title=title,
                    content=chunk,
                    tags=item.get("tags", ""),
                    agent_scope=item.get("agent_scope", "all"),
                    source=default_source,
                    embedding_json=_vector_json(vector),
                    embedding_model=LOCAL_EMBEDDING_MODEL,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
            existing_titles.add(title)
    db.flush()


def agent_config_dict(row: AgentConfig) -> dict:
    return {
        "id": row.id,
        "agent_key": row.agent_key,
        "name": row.name,
        "system_prompt": row.system_prompt,
        "enabled": bool(row.enabled),
        "retrieval_enabled": bool(row.retrieval_enabled),
        "retrieval_top_k": int(row.retrieval_top_k or 5),
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds") if row.updated_at else "",
    }


def list_agent_configs(db: Session) -> list[dict]:
    ensure_agent_configs(db)
    ensure_default_knowledge_chunks(db)
    rows = db.query(AgentConfig).order_by(AgentConfig.id).all()
    return [agent_config_dict(row) for row in rows]


def get_agent_config(db: Session, agent_key: str) -> AgentConfig:
    key = normalize_agent_key(agent_key)
    ensure_agent_configs(db)
    row = db.query(AgentConfig).filter(AgentConfig.agent_key == key).one_or_none()
    if row:
        return row
    row = AgentConfig(**_default_config_row(key), updated_at=datetime.utcnow())
    db.add(row)
    db.flush()
    return row


def update_agent_config(
    db: Session,
    agent_key: str,
    *,
    name: str = "",
    system_prompt: str = "",
    enabled: bool = True,
    retrieval_enabled: bool = True,
    retrieval_top_k: int = 5,
) -> dict:
    row = get_agent_config(db, agent_key)
    row.name = (name or row.name or row.agent_key).strip()[:120]
    row.system_prompt = (system_prompt or "").strip()
    row.enabled = bool(enabled)
    row.retrieval_enabled = bool(retrieval_enabled)
    row.retrieval_top_k = max(1, min(int(retrieval_top_k or 5), 12))
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return agent_config_dict(row)


def _tokens(text: str) -> list[str]:
    clean = re.sub(r"\s+", "", str(text or "").lower())
    if not clean:
        return []
    chars = list(clean)
    grams = chars + [clean[idx : idx + 2] for idx in range(max(len(clean) - 1, 0))]
    words = re.findall(r"[a-z0-9_]{2,}", str(text or "").lower())
    return grams + words


def local_embedding(text: str) -> list[float]:
    vector = [0.0] * LOCAL_EMBEDDING_DIM
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % LOCAL_EMBEDDING_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embed_text(text: str) -> tuple[list[float], str]:
    clean = str(text or "").strip()
    if not clean:
        return local_embedding(""), LOCAL_EMBEDDING_MODEL
    try:
        return call_ark_embedding(clean), ark_embedding_model()
    except (ArkNotConfigured, Exception):
        return local_embedding(clean), LOCAL_EMBEDDING_MODEL


def _vector_json(vector: list[float]) -> str:
    return json.dumps([round(float(value), 8) for value in vector], separators=(",", ":"))


def _load_vector(raw: str) -> list[float]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [float(value) for value in data if isinstance(value, (int, float))]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def lexical_overlap_score(query: str, text: str) -> float:
    query_tokens = set(_tokens(query))
    if not query_tokens:
        return 0.0
    text_tokens = set(_tokens(text))
    return len(query_tokens & text_tokens) / max(len(query_tokens), 1)


def split_knowledge_content(content: str) -> list[str]:
    text = str(content or "").strip()[:MAX_KNOWLEDGE_CHARS]
    if not text:
        return []
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs or [text]:
        if len(current) + len(para) + 2 <= CHUNK_SIZE:
            current = f"{current}\n\n{para}".strip()
            continue
        if current:
            chunks.append(current)
        if len(para) <= CHUNK_SIZE:
            current = para
            continue
        start = 0
        while start < len(para):
            chunks.append(para[start : start + CHUNK_SIZE])
            start += max(CHUNK_SIZE - CHUNK_OVERLAP, 1)
        current = ""
    if current:
        chunks.append(current)
    return chunks


def knowledge_chunk_dict(row: KnowledgeChunk, score: float | None = None) -> dict:
    data = {
        "id": row.id,
        "title": row.title,
        "content": row.content,
        "tags": row.tags,
        "agent_scope": row.agent_scope,
        "source": row.source,
        "embedding_model": row.embedding_model,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else "",
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds") if row.updated_at else "",
    }
    if score is not None:
        data["score"] = round(float(score), 4)
    return data


def create_knowledge_chunks(
    db: Session,
    *,
    title: str,
    content: str,
    tags: str = "",
    agent_scope: str = "all",
    source: str = "manual",
) -> list[dict]:
    clean_title = (title or "未命名知识").strip()[:160]
    scope = normalize_agent_key(agent_scope or "all")
    if scope == "all_":
        scope = "all"
    chunks = split_knowledge_content(content)
    rows: list[KnowledgeChunk] = []
    for index, chunk in enumerate(chunks, 1):
        vector, model = embed_text(f"{clean_title}\n{chunk}")
        row = KnowledgeChunk(
            title=clean_title if len(chunks) == 1 else f"{clean_title} #{index}",
            content=chunk,
            tags=(tags or "").strip(),
            agent_scope=scope,
            source=(source or "manual").strip()[:120],
            embedding_json=_vector_json(vector),
            embedding_model=model,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return [knowledge_chunk_dict(row) for row in rows]


def list_knowledge_chunks(db: Session, limit: int = 200) -> list[dict]:
    rows = db.query(KnowledgeChunk).order_by(KnowledgeChunk.id.desc()).limit(max(1, min(limit, 500))).all()
    return [knowledge_chunk_dict(row) for row in rows]


def delete_knowledge_chunk(db: Session, chunk_id: int) -> bool:
    row = db.query(KnowledgeChunk).filter(KnowledgeChunk.id == int(chunk_id)).one_or_none()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def search_knowledge(db: Session, query: str, agent_key: str = "", top_k: int = 5) -> list[dict]:
    clean_query = str(query or "").strip()
    if not clean_query:
        return []
    key = normalize_agent_key(agent_key or "all")
    vector, model = embed_text(clean_query)
    local_query_vector: list[float] | None = None
    rows = (
        db.query(KnowledgeChunk)
        .filter(or_(KnowledgeChunk.agent_scope == "all", KnowledgeChunk.agent_scope == key))
        .order_by(KnowledgeChunk.id.desc())
        .limit(1000)
        .all()
    )
    scored: list[tuple[float, KnowledgeChunk]] = []
    for row in rows:
        row_model = row.embedding_model or LOCAL_EMBEDDING_MODEL
        if row_model == model:
            query_vector = vector
        elif row_model == LOCAL_EMBEDDING_MODEL:
            if local_query_vector is None:
                local_query_vector = local_embedding(clean_query)
            query_vector = local_query_vector
        else:
            continue
        score = cosine_similarity(query_vector, _load_vector(row.embedding_json))
        score += lexical_overlap_score(clean_query, f"{row.title} {row.tags} {row.content}") * 0.35
        if score >= -0.5:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [knowledge_chunk_dict(row, score) for score, row in scored[: max(1, min(int(top_k or 5), 12))]]


def agent_prompt_with_knowledge(db: Session | None, agent_key: str, fallback_prompt: str, query: str = "") -> tuple[str, list[dict]]:
    if db is None:
        return fallback_prompt, []
    config = get_agent_config(db, agent_key)
    prompt = (config.system_prompt or fallback_prompt).strip() or fallback_prompt
    if not config.enabled:
        return prompt, []
    if not config.retrieval_enabled:
        return prompt, []
    hits = search_knowledge(db, query or prompt, agent_key, config.retrieval_top_k)
    if not hits:
        return prompt, []
    knowledge_text = "\n".join(
        f"- [{item['title']}] {item['content'][:700]}" for item in hits
    )
    return f"{prompt}\n\n【可引用知识库】\n{knowledge_text}", hits
