"""被控端接入包清单与完整性校验数据。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Mapping


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_package_manifest(
    entries: Mapping[str, bytes],
    package_type: str,
    device_id: str = "",
    signature_status: str = "unsigned",
    generated_at: datetime | None = None,
) -> dict:
    generated_at = generated_at or datetime.utcnow()
    files = []
    for name in sorted(entries):
        data = entries[name]
        files.append(
            {
                "path": name.replace("\\", "/"),
                "size_bytes": len(data),
                "sha256": sha256_hex(data),
            }
        )
    return {
        "schema_version": 1,
        "package_type": package_type,
        "device_id": device_id,
        "signature_status": signature_status,
        "generated_at": generated_at.isoformat(sep=" ", timespec="seconds"),
        "file_count": len(files),
        "files": files,
    }
