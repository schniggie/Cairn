from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import urllib.request

"""项目完成时的 webhook 通知。

配置走服务端环境变量，未配置则直接跳过：
  * CAIRN_WEBHOOK_URL    —— 接收通知的地址（不设置就不通知）
  * CAIRN_WEBHOOK_SECRET —— 可选，设置后对请求体做 HMAC-SHA256 签名，
                            放在请求头 X-Cairn-Signature: sha256=<hex>，供机器人侧校验来源。

通知在后台线程里发送，失败只记日志，绝不影响项目 complete 本身。
"""

LOG = logging.getLogger(__name__)
ENV_URL = "CAIRN_WEBHOOK_URL"
ENV_SECRET = "CAIRN_WEBHOOK_SECRET"
SIGNATURE_HEADER = "X-Cairn-Signature"


def notify_project_completed(payload: dict) -> None:
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        return
    secret = os.environ.get(ENV_SECRET, "")
    thread = threading.Thread(
        target=_deliver,
        args=(url, secret, dict(payload)),
        name="cairn-webhook",
        daemon=True,
    )
    thread.start()


def _deliver(url: str, secret: str, payload: dict) -> None:
    project_id = payload.get("project_id")
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "cairn-webhook"}
        if secret:
            signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers[SIGNATURE_HEADER] = f"sha256={signature}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            LOG.info("webhook delivered project=%s status=%s", project_id, response.status)
    except Exception as exc:  # 通知是尽力而为，任何异常都不能冒泡
        LOG.warning("webhook delivery failed project=%s error=%s", project_id, exc)
