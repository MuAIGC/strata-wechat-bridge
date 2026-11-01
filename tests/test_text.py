"""test_text.py — 验证微信侧的 markdown 过滤与引用消息(ref_msg)移植。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_text.py
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
from text_filter import filter_markdown
from weixin_api import text_of


def test_filter() -> None:
    assert filter_markdown("**加粗中文**") == "加粗中文"
    assert filter_markdown("*斜体中文*") == "斜体中文"
    assert filter_markdown("**bold english**") == "**bold english**"
    assert filter_markdown("##### 小标题") == "小标题"
    assert filter_markdown("###### 小标题") == "小标题"
    assert filter_markdown("## 一级标题") == "## 一级标题"
    assert filter_markdown("看图 ![配图](https://x/y.png)") == "看图"
    assert filter_markdown("~~划掉~~") == "划掉"
    assert filter_markdown("- 列表项") == "- 列表项"
    assert filter_markdown("| a | b |\n|---|---|") == "| a | b |\n|---|---|"
    assert filter_markdown("---") == "---"
    code = "```python\n**不是中文粗体** x = 1\n```"
    assert filter_markdown(code) == code, filter_markdown(code)
    mixed = "```bash\necho hi\n```\n**中文重点**\n普通句子 *中文斜体*"
    assert filter_markdown(mixed) == "```bash\necho hi\n```\n中文重点\n普通句子 中文斜体"
    print("→ markdown 过滤规则跟插件一致(代码块/表格/英文粗体保留,中文强调与图片去掉)✓")


def test_ref() -> None:
    msg = {"item_list": [{"type": 1, "text_item": {"text": "再补一句"},
                          "ref_msg": {"message_item": {"msg_id": "m1", "text_item": {"text": "被引用的话"}}}}]}
    got = text_of(msg)
    assert "[引用]被引用的话" in got and "再补一句" in got, got
    only_id = {"item_list": [{"type": 1, "text_item": {"text": "看这个"},
                              "ref_msg": {"svr_id": "svr-9"}}]}
    assert "引用消息 id=svr-9" in text_of(only_id), text_of(only_id)
    print("→ 引用消息(ref_msg)带进指令,只有 id 时也标注 ✓")


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.sent.append(text)
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
        self.calls: list[str] = []

    def api_key(self) -> str:
        return "stub"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        self.calls.append(task)
        return RunResult(ok=True, text="**重点** 中文\n**english bold**", session_id="s-1", exit_code=0)


def test_ref_media_and_filtered_reply() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["relay"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()          # type: ignore[assignment]
    bridge.api = FakeApi()              # type: ignore[assignment]
    bridge.runner = StubRunner()        # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")

    original = B.download_media
    B.download_media = lambda item, cdn_base="", label="media": b"png-bytes"  # type: ignore[assignment]
    try:
        bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx",
                       "message_id": "m-ref-1",
                       "item_list": [{"type": 1, "text_item": {"text": "这张图里是什么"},
                                      "ref_msg": {"message_item": {"type": 2,
                                                                   "image_item": {"media": {"full_url": "https://cdn/x"}}}}}]})
        time.sleep(1.4)
        assert bridge.runner.calls and "[引用]" in bridge.runner.calls[0], bridge.runner.calls
        assert "附件已存:inbound" in bridge.runner.calls[0], bridge.runner.calls[0]
        print("→ 引用里带的图片也被下载落盘 ✓")

        assert bridge.api.sent == ["重点 中文\n**english bold**"], bridge.api.sent
        print("→ 回复发出去前过了 markdown 过滤 ✓")
    finally:
        B.download_media = original     # type: ignore[assignment]


if __name__ == "__main__":
    test_filter()
    test_ref()
    test_ref_media_and_filtered_reply()
    print("\n文本与引用移植通过。")
