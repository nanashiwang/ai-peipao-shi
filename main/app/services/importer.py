"""导入服务。

负责把 CSV / XLSX 的原始行数据转成数据库里的家庭和消息记录。
"""

import csv
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from app.models import Family, RawMessage
from app.services.scenario import detect_checkin


FIELD_ALIASES = {
    "family_id": ["family_id", "家庭编号", "家庭ID"],
    "parent_nickname": ["parent_nickname", "家长昵称"],
    "child_grade": ["child_grade", "孩子年级"],
    "coach_name": ["coach_name", "陪跑师"],
    "message_time": ["message_time", "聊天时间", "时间"],
    "speaker": ["speaker", "说话人"],
    "content": ["content", "消息内容", "内容"],
    "source": ["source", "群/单聊来源", "来源"],
    "checkin_status": ["checkin_status", "打卡状态"],
}


# 按字段别名读取值，统一处理中文表头和英文表头。
def _get(row: dict, field: str, default: str = "") -> str:
    for key in FIELD_ALIASES[field]:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return default


# 把导入文本转成 datetime；没有值时回退到当前时间。
def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.utcnow()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.utcnow()


# 支持 CSV 和 XLSX 两种上传格式。
def rows_from_upload(filename: str, data: bytes) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        text = data.decode("utf-8-sig")
        return list(csv.DictReader(StringIO(text)))
    if suffix in {".xlsx", ".xlsm"}:
        wb = load_workbook(BytesIO(data), data_only=True)
        ws = wb.active
        headers = [str(cell.value).strip() if cell.value is not None else "" for cell in next(ws.iter_rows(max_row=1))]
        rows = []
        for raw in ws.iter_rows(min_row=2, values_only=True):
            rows.append({headers[i]: raw[i] for i in range(len(headers))})
        return rows
    raise ValueError("只支持 CSV / XLSX")


# 把导入行写入数据库，同时自动补齐家庭信息和打卡状态。
def import_rows(db: Session, rows: list[dict]) -> dict:
    families_seen = set()
    family_cache = {family.family_id: family for family in db.query(Family).all()}
    imported = 0
    for row in rows:
        family_id = _get(row, "family_id")
        content = _get(row, "content")
        if not family_id or not content:
            continue
        family = family_cache.get(family_id)
        if not family:
            family = Family(
                family_id=family_id,
                parent_nickname=_get(row, "parent_nickname", family_id),
                child_grade=_get(row, "child_grade"),
                coach_name=_get(row, "coach_name"),
            )
            db.add(family)
            family_cache[family_id] = family
        else:
            family.parent_nickname = family.parent_nickname or _get(row, "parent_nickname", family_id)
            family.child_grade = family.child_grade or _get(row, "child_grade")
            family.coach_name = family.coach_name or _get(row, "coach_name")

        checkin = _get(row, "checkin_status") or detect_checkin(content)
        db.add(
            RawMessage(
                family_id=family_id,
                message_time=_parse_time(_get(row, "message_time")),
                speaker=_get(row, "speaker"),
                content=content,
                source=_get(row, "source", "导入"),
                checkin_status=checkin,
                is_effective="Y" if len(content) >= 2 else "N",
            )
        )
        families_seen.add(family_id)
        imported += 1
    db.commit()
    return {"families": len(families_seen), "messages": imported}
