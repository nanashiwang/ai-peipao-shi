"""Container-friendly health probe with optional webhook alerting."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def load_health(url: str, timeout: int) -> tuple[bool, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return False, {"ok": False, "error": str(exc)}
    return bool(payload.get("ok")), payload


def send_alert(webhook_url: str, payload: dict, timeout: int) -> None:
    if not webhook_url:
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def run_once(env: dict | None = None) -> int:
    source = env or os.environ
    url = source.get("HEALTH_PROBE_URL", "http://127.0.0.1:8000/health")
    timeout = env_int("HEALTH_PROBE_TIMEOUT_SECONDS", 5)
    webhook_url = source.get("ALERT_WEBHOOK_URL", "")

    ok, payload = load_health(url, timeout)
    status = "ok" if ok else "critical"
    line = {"status": status, "target": url, "health": payload}
    print(json.dumps(line, ensure_ascii=False), flush=True)
    if ok:
        return 0

    alert = {
        "title": "AI陪跑师健康检查异常",
        "status": status,
        "target": url,
        "health": payload,
        "ts": int(time.time()),
    }
    try:
        send_alert(webhook_url, alert, timeout)
    except (OSError, urllib.error.URLError) as exc:
        print(json.dumps({"status": "alert_failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
    return 1


def main() -> None:
    interval = env_int("HEALTH_PROBE_INTERVAL_SECONDS", 60)
    run_forever = os.getenv("HEALTH_PROBE_ONCE", "").strip().lower() not in {"1", "true", "yes"}
    while True:
        run_once()
        if not run_forever:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
