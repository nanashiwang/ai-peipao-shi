"""生成被控端发布目录的 package_manifest.json。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rpa.package_manifest import build_package_manifest


EXCLUDED_NAMES = {"package_manifest.json", "STOP_RPA"}
EXCLUDED_DIRS = {".venv", ".venv-build-rpa", "__pycache__", "logs"}


def collect_entries(root: Path) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts) or path.name in EXCLUDED_NAMES:
            continue
        entries["/".join(rel_parts)] = path.read_bytes()
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="生成被控端接入包完整性清单")
    parser.add_argument("--root", required=True, help="发布目录")
    parser.add_argument("--package-type", default="rpa-client-exe")
    parser.add_argument("--device-id", default="")
    parser.add_argument("--signature-status", default="unsigned")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    manifest = build_package_manifest(
        collect_entries(root),
        package_type=args.package_type,
        device_id=args.device_id,
        signature_status=args.signature_status,
    )
    (root / "package_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
