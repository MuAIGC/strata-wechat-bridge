"""test_agent.py — 验证对话档/编程档的切换:人设补丁、权限、配置持久化。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_agent.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import json
import tempfile
from pathlib import Path

import bridge as B


class FakeApi:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.texts.append(text)
        return {"ret": 0}

    def send_typing(self, *a, **k) -> dict:
        return {"ret": 0}


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "wechat.config.json"
    cfg_path.write_text(json.dumps({
        "workspace": str(tmp),
        "allow_from": [],
        "chat_mode": True,
        "agent": {"extra_patch": str(B.CODING_PATCH), "permission_mode": "workspace-write"},
    }), encoding="utf-8")

    cfg = B.load_config(cfg_path)
    bridge = B.WeChatBridge(cfg, config_path=cfg_path)
    bridge.api = FakeApi()              # type: ignore[assignment]

    assert bridge.mode == "chat"
    assert bridge.runner.cfg["extra_patch"] == str(B.CHAT_PATCH), bridge.runner.cfg["extra_patch"]
    assert bridge.runner.cfg["permission_mode"] == "read-only"
    assert Path(B.CHAT_PATCH).is_file(), "对话档补丁文件不存在"
    patch = Path(B.CHAT_PATCH).read_text(encoding="utf-8")
    assert "system-prompt" in patch and "personaPrefix" in patch, "对话档没改人设"
    assert "不要单字" in patch and "三四句" in patch, "人设没要求正常对话长度(手机上之前只会回'好的'/'是的')"
    for dead in ("tool-bash", "tool-pwsh", "tool-subagent", "tool-goal", "tool-todo", "plan-mode"):
        assert f"id: {dead}" in patch, f"对话档没关掉 {dead}"
    # 只读工具留着:手机上问机器上的事实时它能看工作目录(read-only 沙箱改不动东西)
    for alive in ("tool-fs", "tool-fs-search"):
        assert f"id: {alive}" not in patch, f"对话档不该关掉只读工具 {alive}"
    print("→ 对话档补丁:聊天人设(要求正常对话长度)+ 关掉写/跑命令类工具,保留只读 ✓")

    bridge._command("wx-me", "agent", "/agent code", "ctx")
    assert bridge.mode == "code"
    assert bridge.runner.cfg["extra_patch"] == str(B.CODING_PATCH)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["chat_mode"] is False
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["agent"]["extra_patch"] == str(B.CODING_PATCH)
    assert any("编程档" in t for t in bridge.api.texts), bridge.api.texts
    print("→ /agent code 切到编程档并写回配置 ✓")

    bridge._command("wx-me", "agent", "/agent chat", "ctx")
    assert bridge.mode == "chat"
    assert bridge.runner.cfg["permission_mode"] == "read-only"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["chat_mode"] is True
    assert any("对话档" in t for t in bridge.api.texts), bridge.api.texts
    print("→ /agent chat 切回对话档 ✓")

    bridge._command("wx-me", "status", "/status", "ctx")
    assert "对话(chat" in bridge.api.texts[-1], bridge.api.texts[-1]
    print("→ /status 显示当前档 ✓")

    # 对话档下 /mode 被拒(固定 read-only)
    bridge._command("wx-me", "mode", "/mode danger-full-access", "ctx")
    assert "对话档固定 read-only" in bridge.api.texts[-1], bridge.api.texts[-1]
    assert bridge.sessions.get_mode("wx-me") is None, "对话档不该记住提权档位"
    print("→ 对话档拒绝 /mode 提权 ✓")

    # 对话档下不扫目录、不写文件:交付物判定应该永远是空
    assert bridge._deliverables("wx-me", [], None) == []
    assert bridge._deliverables("wx-me", [B.TRANSCRIPT_NAME], None) == []
    print("→ 对话档没有交付物噪音 ✓")


class RecordingRunner:
    cfg = {"serve_url": "x", "patch_dir": ""}

    def __init__(self) -> None:
        self.tasks: list[str] = []

    def api_key(self) -> str:
        return "stub"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None):
        from agent_runner import RunResult
        self.tasks.append(task)
        return RunResult(ok=True, text="好", session_id=None, exit_code=0)


def test_chat_session_choice() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["chat_mode"] = True
    cfg["shared_session"] = True
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.sessions = B.SessionStore(tmp / "sessions.json")
    import os
    os.environ["DSH_SESSION_ID"] = "session-desktop-live"
    assert bridge._session_for("wx-me") is None, "对话档不该去接桌面那条(带 agent preset,接不了)"
    print("→ 对话档不去接桌面会话,直接开微信自己的会话 ✓")


def test_chat_context() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "wechat.config.json"
    cfg_path.write_text(json.dumps({"workspace": str(tmp), "allow_from": [], "chat_mode": True}),
                        encoding="utf-8")
    cfg = B.load_config(cfg_path)
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg, config_path=cfg_path)
    bridge.api = FakeApi()              # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")
    runner = RecordingRunner()
    bridge.runner = runner              # type: ignore[assignment]

    bridge.transcript("wx-me", "手机→微信", "上次聊的是微信桥")
    bridge._run_task("wx-me", "接着说", "ctx")
    assert runner.tasks and "最近对话" in runner.tasks[0], runner.tasks
    assert "上次聊的是微信桥" in runner.tasks[0], runner.tasks[0]
    assert "机器现状" in runner.tasks[0], runner.tasks[0]
    assert "还没有活动" in runner.tasks[0], runner.tasks[0]   # 没活动时别报天文数字
    print("→ 对话档把最近对话和机器现状垫进指令(换会话也不丢上下文) ✓")

    bridge.apply_agent_mode("code")
    bridge.runner = runner              # 切档会重建 runner,测试里换回桩
    bridge._run_task("wx-me", "干活", "ctx")
    assert runner.tasks[-1] == "干活", runner.tasks[-1]
    print("→ 编程档不垫背景,指令原样过去 ✓")


if __name__ == "__main__":
    main()
    test_chat_session_choice()
    test_chat_context()
    print("\n档位切换通过。")
