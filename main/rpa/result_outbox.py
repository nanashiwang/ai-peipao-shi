"""Durable local queue for RPA result callbacks.

The RPA may have already pressed send and verified the message locally. If the
result callback to the server fails, this module keeps the callback payload on
disk so the next polling cycle can retry it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path


def result_outbox_dir(config: dict, root: Path) -> Path:
    raw = str(config.get("result_outbox_dir") or "result_outbox").strip()
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def new_client_result_id(config: dict, task_id: int) -> str:
    device_id = str(config.get("device_id") or "local").strip() or "local"
    return f"{device_id}-{task_id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


def enqueue_result(
    config: dict,
    root: Path,
    task_id: int,
    path: str,
    payload: dict,
    error: str = "",
) -> Path:
    outbox = result_outbox_dir(config, root)
    outbox.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task_id,
        "path": path,
        "payload": payload,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "attempts": 0,
        "last_error": str(error or ""),
    }
    filename = f"{int(time.time() * 1000)}_{task_id}_{uuid.uuid4().hex[:10]}.json"
    final_path = outbox / filename
    temp_path = final_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(final_path)
    return final_path


def pending_result_files(config: dict, root: Path) -> list[Path]:
    outbox = result_outbox_dir(config, root)
    if not outbox.exists():
        return []
    return sorted(path for path in outbox.glob("*.json") if path.is_file())


def load_result_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def mark_result_retry(path: Path, record: dict, error: str) -> None:
    record = {**record}
    record["attempts"] = int(record.get("attempts") or 0) + 1
    record["last_error"] = str(error or "")
    record["last_attempt_at"] = datetime.utcnow().isoformat(timespec="seconds")
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def remove_result_record(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
