"""test_relay.py — 验证 relay 模式(指令转给桌面对话)和 send.py(桌面这一侧回微信)。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_relay.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import json
import tempfile
import time
from pathlib import Path

import bridge as B
import send as S
from agent_runner import RunResult


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.sent.append((to_user_id, text, context_token))
        return {"ret": 0, "message_id": "m"}

    def send_typing(self, *a, **k) -> dict:
        return {"ret": 0}


class FakeStore:
    def load(self) -> dict:
        return {"token": "t", "base_url": "https://example.invalid"}

    def token(self) -> str:
        return "t"

    def base_url(self) -> str:
        return "https://example.invalid"

    def sync_buf(self) -> str:
        return ""

    def set_sync_buf(self, buf: str) -> None:
        pass

    def local_tokens(self) -> list[str]:
        return ["t"]


class NoRunner:
    cfg = {"serve_url": "x", "patch_dir": ""}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def api_key(self) -> str:
        return "stub"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        self.calls.append(task)
        return RunResult(ok=True, text="不该被调用", session_id="s", exit_code=0)


def test_relay() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["shared_session"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["relay"] = True
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()           # type: ignore[assignment]
    bridge.api = FakeApi()               # type: ignore[assignment]
    bridge.runner = NoRunner()           # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")

    bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx-relay",
                   "message_id": "m-r1",
                   "item_list": [{"type": 1, "text_item": {"text": "帮我改一下 config"}}]})
    time.sleep(1.0)

    assert bridge.runner.calls == [], "relay 模式还跑了本机 agent"
    inbox = (tmp / B.INBOX_NAME).read_text(encoding="utf-8")
    assert "帮我改一下 config" in inbox and "ctx-relay" in inbox, inbox
    assert any("已转到桌面对话" in t for _, t, _ in bridge.api.sent), bridge.api.sent
    assert bridge.sessions.ctx("wx-me") == "ctx-relay"
    print("→ relay:指令写进 wechat-inbox.md(带 context_token),不跑 headless ✓")

    # /transcript 能看最近几条
    bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx-relay",
                   "message_id": "m-r2", "item_list": [{"type": 1, "text_item": {"text": "/transcript"}}]})
    time.sleep(0.8)
    last = bridge.api.sent[-1][1]
    assert "手机→微信" in last and "帮我改一下 config" in last, last
    print("→ /transcript 回最近对话 ✓")


def test_send_script() -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "weixin-account.json").write_text(json.dumps(
        {"token": "tok-1", "base_url": "https://example.invalid", "ilink_user_id": "peer-1"}), encoding="utf-8")
    (tmp / "wechat.config.json").write_text(json.dumps(
        {"allow_from": ["peer-1"], "bot_agent": "Strata/dsh", "workspace": str(tmp)}), encoding="utf-8")
    (tmp / "sessions.json").write_text(json.dumps(
        {"peer-1": {"session_id": "s", "context_token": "ctx-9"}}), encoding="utf-8")

    sent: list[tuple[str, str, str | None]] = []

    class Api:
        def __init__(self, base, token, agent):
            assert token == "tok-1"

        def send_text(self, peer, text, ctx=None):
            sent.append((peer, text, ctx))
            return {"ret": 0, "message_id": "m-1"}

    S.WeixinApi = Api                       # type: ignore[assignment]
    S.ACCOUNT_PATH = tmp / "weixin-account.json"
    S.CONFIG_PATH = tmp / "wechat.config.json"
    S.SESSIONS_PATH = tmp / "sessions.json"

    resp = S.send("桌面这边回的一句话")
    assert resp["ret"] == 0
    assert sent == [("peer-1", "桌面这边回的一句话", "ctx-9")], sent
    print("→ send.py 用桥的凭证和 context_token 回到同一条微信会话 ✓")

    (tmp / "weixin-account.json").write_text("{}", encoding="utf-8")
    try:
        S.send("不该发出去")
        raise AssertionError("没登录也发出去了")
    except RuntimeError as e:
        assert "还没扫码登录" in str(e)
    print("→ 没登录时 send.py 明确报错 ✓")


def test_relay_toggle() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "wechat.config.json"
    cfg_path.write_text(json.dumps({"allow_from": [], "relay": False, "workspace": str(tmp),
                                   "chat_mode": False, "chat_context": False}), encoding="utf-8")
    cfg = B.load_config(cfg_path)
    cfg["shared_session"] = False
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg, config_path=cfg_path)
    bridge.store = FakeStore()           # type: ignore[assignment]
    bridge.api = FakeApi()               # type: ignore[assignment]
    bridge.runner = NoRunner()           # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")

    def msg(text: str, mid: str) -> dict:
        return {"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx-t",
                "message_id": mid, "item_list": [{"type": 1, "text_item": {"text": text}}]}

    bridge.handle(msg("本机执行这条", "m-t1"))
    time.sleep(1.0)
    assert bridge.runner.calls == ["本机执行这条"], bridge.runner.calls

    bridge._command("wx-me", "relay", "/relay on", "ctx-t")
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["relay"] is True
    assert any("relay 模式" in t for _, t, _ in bridge.api.sent), bridge.api.sent

    # 手机上打错 /rely on 也能生效(别名)
    bridge._command("wx-me", "relay", "/relay off", "ctx-t")
    bridge.handle(msg("/rely on", "m-t3"))
    time.sleep(0.8)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["relay"] is True, "别名 /rely 没生效"

    bridge.handle(msg("这条应该转给桌面", "m-t2"))
    time.sleep(1.0)
    assert bridge.runner.calls == ["本机执行这条"], "切了 relay 还跑本机 agent"
    assert "这条应该转给桌面" in (tmp / B.INBOX_NAME).read_text(encoding="utf-8")

    # /history 是 /transcript 的别名
    bridge.handle(msg("/history", "m-t4"))
    time.sleep(0.8)
    assert "手机→微信" in bridge.api.sent[-1][1], bridge.api.sent[-1]
    print("→ 别名:/rely→/relay、/history→/transcript ✓")

    bridge._command("wx-me", "relay", "/relay off", "ctx-t")
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["relay"] is False
    assert any("快回模式" in t for _, t, _ in bridge.api.sent), bridge.api.sent
    print("→ /relay on|off 从手机上就能切,配置写回文件,行为跟着变 ✓")


if __name__ == "__main__":
    test_relay()
    test_relay_toggle()
    test_send_script()
    print("\nrelay 与桌面侧回复通过。")
