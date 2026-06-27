# -*- coding: utf-8 -*-
"""被控端连通客户端（最小版，纯标准库）。

打包成 exe 后对方双击即用：读同目录 config.json → 定时心跳 + 领取本机任务 + 回写结果。
真实发送（企微窗口操控 + ARK 云端定位）是后续集成项；本版先打通"连通/领取/回写"链路，
用于验证 PyInstaller 打包与被控端分发流程。
"""
import json
import os
import sys
import time
import urllib.request


def base_dir():
    # 打包成 exe 后，配置文件放在 exe 同目录。
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(base_dir(), "config.json"), encoding="utf-8") as f:
        return json.load(f)


def req(base, path, method="GET", payload=None, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(base.rstrip("/") + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def tick(cfg):
    base = cfg["api_base_url"]
    device_id = cfg["device_id"]
    headers = {"X-Device-Id": device_id, "X-Device-Token": cfg["device_token"]}
    convs = cfg.get("conversations", [])
    req(base, f"/api/devices/{device_id}/heartbeat", "POST",
        {"wecom_ok": "Y", "detail": "", "conversations": convs}, headers)
    tasks = req(base, f"/api/devices/{device_id}/claim?limit=5", "POST", None, headers)
    if tasks:
        print(f"领到 {len(tasks)} 条任务: {[t['content'][:20] for t in tasks]}")
        for t in tasks:
            # TODO: 真实发送（企微操控+ARK定位）在此集成；当前先回写占位。
            req(base, f"/api/send-tasks/{t['id']}/result", "POST",
                {"status": "dry_run", "detail": "连通客户端占位", "device_id": device_id}, headers)
    else:
        print("无待发送任务")


def main():
    cfg = load_config()
    print(f"被控端启动: device={cfg['device_id']} server={cfg['api_base_url']} 负责会话={cfg.get('conversations', [])}")
    once = "--once" in sys.argv
    interval = int(cfg.get("poll_interval_seconds", 15))
    while True:
        try:
            tick(cfg)
        except Exception as exc:
            print(f"循环出错: {exc}")
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
