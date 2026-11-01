"""test_recover.py — 验证凭证失效时桥会自己重新扫码,不用重启进程。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_recover.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import tempfile
import time
from pathlib import Path

import bridge as B
from agent_runner import RunResult


class Api:
    base_url = "https://example.invalid/"
    token = "old-token"

    def __init__(self) -> None:
        self.polls = 0
        self.sent: list[str] = []
        self.qr_calls = 0

    def get_config(self) -> dict:
        return {"ret": 0, "typing_ticket": "t"}

    def notify_start(self) -> dict:
        return {"ret": 0}

    def notify_stop(self) -> dict:
        return {"ret": 0}

    def get_updates(self, buf: str, timeout: float = 35.0) -> dict:
        self.polls += 1
        if self.polls == 1:
            return {"ret": -4001, "errcode": -4001, "errmsg": "token stale"}
        if self.polls == 2:
            return {"ret": 0,
                    "msgs": [{"message_id": "m-recover", "from_user_id": "wx-me",
                              "message_type": 1, "context_token": "ctx",
                              "item_list": [{"type": 1, "text_item": {"text": "恢复后还能收"}}]}],
                    "get_updates_buf": "buf-1"}
        raise KeyboardInterrupt

    def fetch_qrcode(self, local_tokens, bot_type="3") -> dict:
        self.qr_calls += 1
        return {"ret": 0, "qrcode": "qr-recover", "qrcode_img_content": "https://liteapp/q?x=1"}

    def poll_qrcode_status(self, qrcode, verify_code=None, base_url="", timeout=35.0) -> dict:
        return {"status": "confirmed", "bot_token": "fresh-token", "ilink_bot_id": "bot-2",
                "baseurl": "https://example.invalid", "ilink_user_id": "wx-me"}

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.sent.append(text)
        return {"ret": 0, "message_id": "m"}

    def send_typing(self, *a, **k) -> dict:
        return {"ret": 0}


class Store:
    def __init__(self) -> None:
        self.data = {"token": "old-token", "base_url": "https://example.invalid"}
        self.buf = ""

    def load(self) -> dict:
        return self.data

    def token(self) -> str:
        return self.data["token"]

    def base_url(self) -> str:
        return self.data["base_url"]

    def sync_buf(self) -> str:
        return self.buf

    def set_sync_buf(self, buf: str) -> None:
        self.buf = buf

    def local_tokens(self) -> list[str]:
        return [self.data["token"]]

    def save(self, data: dict) -> None:
        self.data = dict(data)


class Runner:
    cfg = {"serve_url": "http://127.0.0.1:18500/v1/models", "patch_dir": ""}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def api_key(self) -> str:
        return "stub"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        self.calls.append(task)
        return RunResult(ok=True, text=f"收到:{task}", session_id="s-1", exit_code=0)


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    B.QR_PATH = tmp / "qr.png"          # 别覆盖真实登录用的二维码
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["relay"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["send_deliverables"] = False
    cfg["auto_relogin"] = True

    bridge = B.WeChatBridge(cfg)
    store = Store()
    api = Api()
    bridge.store = store                # type: ignore[assignment]
    bridge.api = api                    # type: ignore[assignment]
    bridge.runner = Runner()            # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")

    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        pass

    wait = 1.5
    time.sleep(wait)

    assert api.qr_calls == 1, f"没自动重新取二维码:{api.qr_calls}"
    assert store.token() == "fresh-token", store.data
    assert bridge.api.token == "fresh-token", getattr(bridge.api, "token", None)
    assert bridge.runner.calls == ["恢复后还能收"], bridge.runner.calls
    assert any("收到:恢复后还能收" in t for t in api.sent), api.sent
    print("→ 凭证失效后自动换码登录,新 token 生效,消息继续处理 ✓")

    cfg2 = dict(cfg, auto_relogin=False)
    bridge2 = B.WeChatBridge(cfg2)
    api2 = Api()
    store2 = Store()
    bridge2.store = store2              # type: ignore[assignment]
    bridge2.api = api2                  # type: ignore[assignment]
    bridge2.runner = Runner()           # type: ignore[assignment]
    bridge2.sessions = B.SessionStore(tmp / "sessions2.json")
    # 关掉自动换码:应该走退避而不是扫码(用短睡眠桩避免真等 5 分钟)
    time.sleep = lambda s: None         # type: ignore[assignment]
    try:
        bridge2.run_forever()
    except KeyboardInterrupt:
        pass
    assert api2.qr_calls == 0, "关了 auto_relogin 还去扫码了"
    assert store2.token() == "old-token", "关了开关还换了 token"
    print("→ auto_relogin=false 时只退避,不自动扫码 ✓")
    print("\n凭证恢复路径通过。")


if __name__ == "__main__":
    main()
