"""test_local.py — 不连微信,用一个假的 iLink API 把"收消息 → 本机执行 → 回复"走一遍。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_local.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import tempfile
import time
import types
from pathlib import Path

import bridge as B
from agent_runner import RunResult


class StubRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.modes: list[str | None] = []
        self.cfg = {"serve_url": "http://127.0.0.1:18500/v1/models", "patch_dir": ""}

    def api_key(self) -> str:
        return "stub-key"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        self.calls.append((task, session_id))
        self.modes.append(permission_mode)
        if cancel is not None and cancel.is_set():
            return RunResult(ok=False, text="", session_id=session_id, exit_code=-1, error="被 /abort 取消")
        return RunResult(ok=True, text=f"已执行:{task}", session_id="session-stub-1", exit_code=0)


class FakeStore:
    def __init__(self) -> None:
        self.data = {"token": "stub-token", "base_url": "https://example.invalid",
                    "ilink_bot_id": "bot-1", "ilink_user_id": "wx-me"}
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


class FakeApi:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.sent: list[tuple[str, str]] = []
        self.typing: list[tuple[str, int]] = []
        self.polls = 0
        self.started = False
        self.stopped = False

    def get_config(self) -> dict:
        return {"ret": 0, "typing_ticket": "ticket-1"}

    def notify_start(self) -> dict:
        self.started = True
        return {"ret": 0}

    def notify_stop(self) -> dict:
        self.stopped = True
        return {"ret": 0}

    def get_updates(self, buf: str, timeout: float = 35.0) -> dict:
        self.polls += 1
        if self.messages:
            msgs = self.messages[:1]
            self.messages = self.messages[1:]
            return {"ret": 0, "msgs": msgs, "get_updates_buf": f"buf-{self.polls}",
                    "longpolling_timeout_ms": 30000}
        raise KeyboardInterrupt   # 让 run_forever 退出

    def send_text(self, to_user_id: str, text: str, context_token=None, run_id=None) -> dict:
        self.sent.append((to_user_id, text))
        return {"ret": 0, "message_id": f"m{len(self.sent)}"}

    def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int = 1) -> dict:
        self.typing.append((ilink_user_id, status))
        return {"ret": 0}


def user_msg(text: str, peer: str = "wx-me", ctx: str = "ctx-1", msg_id: str | None = None) -> dict:
    return {
        "message_id": msg_id or f"m-{int(time.time() * 1000)}-{len(text)}",
        "from_user_id": peer,
        "to_user_id": "bot-1",
        "message_type": 1,
        "message_state": 2,
        "context_token": ctx,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def wait(seconds: float = 1.2) -> None:
    time.sleep(seconds)


def fresh_sessions(bridge: B.WeChatBridge) -> None:
    """每个测试用独立的会话映射文件,别被上次跑的结果污染。"""
    import tempfile
    bridge.sessions = B.SessionStore(Path(tempfile.mktemp(prefix="wx-sessions-")))


def main() -> None:
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["relay"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["workspace"] = str(Path(tempfile.mkdtemp()))   # 别往真实工作目录写对话记录
    cfg["allow_from"] = []
    cfg["shared_session"] = False   # 这个测试看的是独立会话的连续性
    cfg["send_deliverables"] = False   # 这个测试只验收发链路,不去碰真实上传
    bridge = B.WeChatBridge(cfg)
    fresh_sessions(bridge)
    bridge.store = FakeStore()          # type: ignore[assignment]
    bridge.runner = StubRunner()         # type: ignore[assignment]
    api = FakeApi([user_msg("看看目录里有什么")])
    bridge.api = api                     # type: ignore[assignment]

    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        pass

    wait(1.5)
    assert api.started, "没发 notifystart"
    assert api.stopped, "没发 notifystop"
    assert bridge.runner.calls == [("看看目录里有什么", None)], bridge.runner.calls
    assert any("已执行:看看目录里有什么" in t for _, t in api.sent), api.sent
    assert bridge.sessions.get("wx-me") == "session-stub-1", "会话没绑定"
    assert ("wx-me", 1) in api.typing and ("wx-me", 2) in api.typing, "typing 提示没发"
    print("→ 收消息 → 本机执行 → 回复,并带 typing 提示 ✓")
    print("  回复:", [t[:40] for _, t in api.sent])

    # 第二条应该续话
    bridge.handle(user_msg("继续", ctx="ctx-2"))
    wait(1.5)
    assert bridge.runner.calls[-1] == ("继续", "session-stub-1"), bridge.runner.calls[-1]
    print("→ 第二条带上了 session id ✓")

    # /new 清掉
    bridge.handle(user_msg("/new"))
    wait(0.6)
    assert bridge.sessions.get("wx-me") is None
    print("→ /new 清掉会话 ✓")

    # 服务端重投同一条 message_id → 只执行一次
    calls_before = len(bridge.runner.calls)
    dup = user_msg("重投测试", msg_id="om-dup-1")
    bridge.handle(dup)
    wait(1.2)
    bridge.handle(dup)
    wait(1.2)
    assert len(bridge.runner.calls) == calls_before + 1, bridge.runner.calls[calls_before:]
    print("→ 同一 message_id 重投被去重 ✓")

    # 白名单
    cfg2 = dict(cfg, allow_from=["someone-else"])
    bridge2 = B.WeChatBridge(cfg2)
    fresh_sessions(bridge2)
    bridge2.store = FakeStore()          # type: ignore[assignment]
    bridge2.runner = StubRunner()        # type: ignore[assignment]
    api2 = FakeApi([])
    bridge2.api = api2                   # type: ignore[assignment]
    bridge2.handle(user_msg("执行:删掉整个目录"))
    wait(0.6)
    assert not bridge2.runner.calls, "陌生对端竟然被执行了"
    assert any("白名单" in t for _, t in api2.sent), api2.sent
    print("→ 不在 allow_from 的对端被挡住 ✓")

    # /whoami 与 /mode
    bridge.handle(user_msg("/whoami"))
    wait(0.6)
    assert any("wx-me" in t for _, t in api.sent[-1:]), api.sent[-1]
    cfg3 = dict(cfg, allow_mode_switch=True, allow_from=["wx-me"], chat_mode=False)
    bridge3 = B.WeChatBridge(cfg3)
    fresh_sessions(bridge3)
    bridge3.store = FakeStore()          # type: ignore[assignment]
    bridge3.runner = StubRunner()        # type: ignore[assignment]
    bridge3.api = FakeApi([])            # type: ignore[assignment]
    bridge3.handle(user_msg("/mode danger-full-access"))
    wait(0.6)
    assert bridge3.sessions.get_mode("wx-me") == "danger-full-access"
    bridge3.handle(user_msg("跑一条"))
    wait(1.2)
    assert bridge3.runner.modes[-1] == "danger-full-access", bridge3.runner.modes
    print("→ /whoami 与 /mode 生效 ✓")

    # confirm_first:先问,回『/confirm』才跑
    cfg4 = dict(cfg, confirm_first=True)
    bridge4 = B.WeChatBridge(cfg4)
    fresh_sessions(bridge4)
    bridge4.store = FakeStore()          # type: ignore[assignment]
    bridge4.runner = StubRunner()        # type: ignore[assignment]
    api4 = FakeApi([])
    bridge4.api = api4                   # type: ignore[assignment]
    bridge4.handle(user_msg("写个报告"))
    wait(0.6)
    assert not bridge4.runner.calls, "没确认就执行了"
    assert any("要执行" in t for _, t in api4.sent), api4.sent
    bridge4.handle(user_msg("/confirm"))
    wait(1.2)
    assert bridge4.runner.calls == [("写个报告", None)], bridge4.runner.calls
    print("→ confirm_first:回 /confirm 才执行 ✓")

    print("\n本地全链路通过。")


if __name__ == "__main__":
    main()
