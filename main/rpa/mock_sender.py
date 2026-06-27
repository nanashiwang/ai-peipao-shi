"""Mock 发送器。

这个脚本只调用后端接口，不触碰企业微信窗口，适合本地联调和演示。
"""

import json
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:8000"


# 发起一个最小化的 JSON 请求。
def request_json(path: str, method: str = "GET"):
    req = Request(f"{BASE_URL}{path}", method=method)
    with urlopen(req, timeout=10) as response:
        data = response.read().decode("utf-8")
    return json.loads(data)


# 把待发送任务全部发送到网页通讯，便于验证后端回写和前端状态流转。
def main():
    tasks = request_json("/api/send-tasks")
    pending = [task for task in tasks if task["status"] == "pending"]
    for task in pending:
        print(f"[MOCK_RPA] send to {task['target_name']} ({task['family_id']}): {task['content'][:60]}")
        request_json(f"/api/send-tasks/{task['id']}/web-send", method="POST")
    print(f"done, sent {len(pending)} task(s)")


if __name__ == "__main__":
    main()
