"""test_deps_optional.py — 没装 qrcode / pycryptodome 时包还能跑(登录链接 + 对话回复)。

跑法: python tests/test_deps_optional.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge as B
import media as M
from agent_runner import RunResult


class FakeApi:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.texts.append(text)
        return {"ret": 0}

    def send_typing(self, *a, **k) -> dict:
        return {"ret": 0}

    def send_item(self, to_user_id, item, context_token=None) -> dict:
        raise AssertionError("没装依赖时不该尝试发媒体")


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
    cfg = {"serve_url": "x", "patch_dir": ""}

    def api_key(self) -> str:
        return "stub"

    def run(self, task, session_id, cwd, on_event=None, cancel=None, permission_mode=None) -> RunResult:
        return RunResult(ok=True, text="在的,你说。", session_id=None, exit_code=0)


def test_qrcode_missing() -> None:
    saved = sys.modules.get("qrcode")
    sys.modules["qrcode"] = None          # 让 import qrcode 抛 ImportError
    try:
        out = B.WeChatBridge.render_qr("https://liteapp.weixin.qq.com/q/x", Path("nope.png"))
        assert out is None, out
        print("→ 没装 qrcode:二维码图片不生成,链接照样能用 ✓")
    finally:
        if saved is not None:
            sys.modules["qrcode"] = saved
        else:
            sys.modules.pop("qrcode", None)


def test_pycryptodome_missing() -> None:
    saved = M.AES
    M.AES = None
    try:
        try:
            M.aes_ecb_encrypt(b"hello", b"0123456789abcdef")
            raise AssertionError("没装 pycryptodome 竟然还能加密")
        except RuntimeError as e:
            assert "pycryptodome" in str(e)
        print("→ 没装 pycryptodome:媒体加解密明确报错,不影响纯文字对话 ✓")
    finally:
        M.AES = saved


def test_chat_works_without_deps() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["relay"] = False
    cfg["chat_mode"] = True
    cfg["chat_context"] = False
    cfg["send_deliverables"] = False
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()           # type: ignore[assignment]
    bridge.api = FakeApi()               # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")
    bridge.runner = StubRunner()         # type: ignore[assignment]

    bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx",
                   "message_id": "m-deps",
                   "item_list": [{"type": 1, "text_item": {"text": "在么"}}]})
    for _ in range(30):
        if bridge.api.texts:
            break
        time.sleep(0.1)
    assert bridge.api.texts == ["在的,你说。"], bridge.api.texts
    print("→ 零依赖也能完成一条纯文字对话 ✓")


if __name__ == "__main__":
    test_qrcode_missing()
    test_pycryptodome_missing()
    test_chat_works_without_deps()
    print("\n可选依赖降级通过。")
