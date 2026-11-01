"""test_session.py — 验证"微信接到桌面当前会话"的接线,以及交付物的跳过规则。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_session.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import os
import tempfile
import time
from pathlib import Path

import bridge as B
from agent_runner import RunResult


class FakeApi:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.texts.append(text)
        return {"ret": 0}

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


class StubRunner:
    cfg = {"serve_url": "http://127.0.0.1:18500/v1/models", "patch_dir": ""}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def api_key(self) -> str:
        return "stub"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        self.calls.append((task, session_id))
        return RunResult(ok=True, text="好了", session_id=session_id or "brand-new", exit_code=0)


def test_skip_rules() -> None:
    ws = Path("C:/ws")
    assert B.is_bridge_artifact(ws / "wechat-bridge" / "sessions.json", ws)
    assert B.is_bridge_artifact(ws / "sessions.json", ws)
    assert B.is_bridge_artifact(ws / "weixin-account.json", ws)
    assert B.is_bridge_artifact(ws / "wechat-bridge" / "weixin-qr.png", ws)
    assert B.is_bridge_artifact(ws / "inbound" / "照片.png", ws)
    assert B.is_bridge_artifact(ws / "bridge.log", ws)
    assert not B.is_bridge_artifact(ws / "报告.txt", ws)
    assert not B.is_bridge_artifact(ws / "docs" / "图.png", ws)
    assert B.is_bridge_artifact((ws / ".." / "外部.txt").resolve(), ws)
    assert B.is_bridge_artifact(ws / B.TRANSCRIPT_NAME, ws)
    print("→ 桥自己的状态文件、附件目录、对话记录、目录外的文件都被跳过 ✓")


def test_transcript() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["relay"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["chat_mode"] = False
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["shared_session"] = False
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()           # type: ignore[assignment]
    bridge.api = FakeApi()               # type: ignore[assignment]
    bridge.runner = StubRunner()         # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")

    bridge._process({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx",
                     "message_id": "m-t1",
                     "item_list": [{"type": 1, "text_item": {"text": "手机发的一句话"}}]})
    for _ in range(30):
        if (tmp / B.TRANSCRIPT_NAME).exists():
            break
        time.sleep(0.1)
    text = (tmp / B.TRANSCRIPT_NAME).read_text(encoding="utf-8")
    assert "手机→微信" in text and "手机发的一句话" in text, text
    assert "微信→手机" in text and "好了" in text, text
    print("→ 每条手机对话都记进 wechat-chat.md,桌面这边能读到同一条记录 ✓")


def test_session_wiring() -> None:
    tmp = Path(tempfile.mkdtemp())
    home = tmp / "home"
    ws = tmp / "ws"
    ws.mkdir()
    slug = B.session_slug(str(ws))
    d = home / "sessions" / slug
    old = d / "session-aaaa"
    cur = d / "session-bbbb"
    old.mkdir(parents=True)
    cur.mkdir(parents=True)
    now = time.time()
    os.utime(old, (now - 600, now - 600))
    os.utime(cur, (now, now))
    os.environ["DSH_HOME"] = str(home)
    os.environ.pop("DSH_SESSION_ID", None)   # 退化成"最近活动的会话目录"这条路径

    assert B.session_slug(str(ws)) == "--" + slug.strip("--") + "--"
    assert B.detect_current_session(str(ws)) == "session-bbbb", B.detect_current_session(str(ws))

    # 环境变量优先:桌面当前对话的 id
    os.environ["DSH_SESSION_ID"] = "session-desktop-live"
    assert B.detect_current_session(str(ws)) == "session-desktop-live"
    os.environ["DSH_SESSION_ID"] = "session-bbbb"
    print("→ 桌面当前会话 id 从 DSH_SESSION_ID 拿,退化成最近活动目录 ✓")

    cfg = B.load_config(B.CONFIG_PATH)
    cfg["workspace"] = str(ws)
    cfg["allow_from"] = []
    cfg["chat_mode"] = False      # 对话档不去接桌面会话;这条测编程档的接线
    cfg["shared_session"] = True
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()           # type: ignore[assignment]
    bridge.api = FakeApi()               # type: ignore[assignment]
    bridge.runner = StubRunner()         # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")

    sid = bridge._session_for("wx-me")
    assert sid == "session-bbbb", sid
    assert bridge.sessions.get("wx-me") == "session-bbbb"
    assert bridge.sessions.is_detached("wx-me") is False
    print("→ 微信第一条指令直接接到桌面当前会话 ✓")

    # /new 之后走独立会话
    bridge._command("wx-me", "new", "/new", "ctx")
    assert bridge.sessions.get("wx-me") is None
    assert bridge.sessions.is_detached("wx-me") is True
    assert bridge._session_for("wx-me") is None
    print("→ /new 之后不再接桌面会话,走独立会话 ✓")

    # /session current 接回来
    bridge._command("wx-me", "session", "/session current", "ctx")
    assert bridge.sessions.get("wx-me") == "session-bbbb"
    assert any("已接到桌面当前会话" in t for t in bridge.api.texts), bridge.api.texts
    print("→ /session current 重新接回桌面当前会话 ✓")

    # /status 里能说清楚接的是哪条
    bridge._command("wx-me", "status", "/status", "ctx")
    assert any("桌面当前对话" in t for t in bridge.api.texts), bridge.api.texts[-1]
    print("→ /status 显示会话来源 ✓")


class PresetFailRunner(StubRunner):
    """第一次(headless 接不了带 agent preset 的桌面会话)失败,第二次(独立会话)成功。"""

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        self.calls.append((task, session_id))
        if session_id == "session-desktop-live":   # 只有桌面那条会话接不了
            return RunResult(ok=False, text="", session_id=None, exit_code=1,
                             error=f'session "{session_id}" runs under agent preset "standard", '
                                   "which the one-shot runner does not compose")
        return RunResult(ok=True, text="好了", session_id=session_id or "phone-session-1", exit_code=0)


def test_preset_fallback() -> None:
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "ws"
    ws.mkdir()
    os.environ["DSH_SESSION_ID"] = "session-desktop-live"
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["workspace"] = str(ws)
    cfg["allow_from"] = []
    cfg["chat_mode"] = False      # 接不上桌面会话的退化路径属于编程档
    cfg["shared_session"] = True
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()           # type: ignore[assignment]
    bridge.api = FakeApi()               # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")
    runner = PresetFailRunner()
    bridge.runner = runner               # type: ignore[assignment]

    bridge._run_task("wx-me", "干活", "ctx")
    assert runner.calls == [("干活", "session-desktop-live"), ("干活", None)], runner.calls
    assert bridge.sessions.get("wx-me") == "phone-session-1", bridge.sessions.data
    assert bridge.sessions.is_detached("wx-me") is True
    assert any("接不上原来的 dsh 会话" in t for t in bridge.api.texts), bridge.api.texts
    # 下一条不再尝试桌面会话
    bridge._run_task("wx-me", "再来", "ctx")
    assert runner.calls[-1] == ("再来", "phone-session-1"), runner.calls[-1]
    print("→ 接不了桌面会话时退化成微信自己的持久会话,并说明原因 ✓")


if __name__ == "__main__":
    test_skip_rules()
    test_session_wiring()
    os.environ.pop("DSH_SESSION_ID", None)
    test_preset_fallback()
    print("\n会话接线通过。")
