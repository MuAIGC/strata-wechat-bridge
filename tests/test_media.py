"""test_media.py — 验证微信 CDN 媒体的解密与落盘(不联网,fetch 用桩)。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_media.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import base64
import secrets
import tempfile
import time
from pathlib import Path

import bridge as B
import media as M
from agent_runner import RunResult
from media import aes_ecb_decrypt, aes_ecb_encrypt, aes_padded_size, download_media, media_item, parse_aes_key, save_media


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to_user_id: str, text: str, context_token=None, run_id=None) -> dict:
        self.sent.append((to_user_id, text))
        return {"ret": 0}

    def send_typing(self, *a, **k) -> dict:
        return {"ret": 0}


class FakeStore:
    def load(self) -> dict:
        return {"token": "t", "base_url": "https://example.invalid", "ilink_bot_id": "bot-1"}

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
        return RunResult(ok=True, text=f"看到了:{task}", session_id="s-1", exit_code=0)


def main() -> None:
    key = secrets.token_bytes(16)
    plain = "这是微信发来的图片内容".encode("utf-8")
    cipher = aes_ecb_encrypt(plain, key)
    assert aes_ecb_decrypt(cipher, key) == plain, "AES-ECB 往返不一致"
    assert len(cipher) % 16 == 0 and len(cipher) == aes_padded_size(len(plain)), "填充长度不对"
    print("→ AES-128-ECB + PKCS7 往返正确(密文", len(cipher), "字节)✓")

    raw_b64 = base64.b64encode(key).decode()          # 图片:base64(16 原始字节)
    hex_b64 = base64.b64encode(key.hex().encode("ascii")).decode()   # 文件/语音:base64(32 位 hex)
    assert parse_aes_key(raw_b64) == key and parse_aes_key(hex_b64) == key, "aes_key 解析不对"
    assert M.aes_key_from_item({"aeskey": key.hex()}) == key, "image_item.aeskey(hex)解析不对"
    print("→ aes_key 三种形态都能解析 ✓")

    fetched: list[str] = []

    def fake_fetch(url: str) -> bytes:
        fetched.append(url)
        return cipher

    got = download_media({"media": {"full_url": "https://cdn.example/img?x=1", "aes_key": raw_b64}},
                         label="image", fetch=fake_fetch)
    assert got == plain and fetched == ["https://cdn.example/img?x=1"], (got, fetched)
    print("→ 按 full_url 下载并解密 ✓")

    got2 = download_media({"media": {"encrypt_query_param": "p/abc?k=1", "aes_key": hex_b64}},
                          cdn_base="https://novac2c.cdn.weixin.qq.com/c2c", label="file", fetch=fake_fetch)
    assert got2 == plain and fetched[-1] == "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=p%2Fabc%3Fk%3D1", fetched[-1]
    print("→ 没有 full_url 时按 encrypt_query_param 拼 /download 地址 ✓")

    assert media_item({"type": 2, "image_item": {"media": {"full_url": "u"}}}) == {"media": {"full_url": "u"}}
    assert media_item({"type": 4, "file_item": {"media": {"full_url": "u"}, "file_name": "a.txt"}})["file_name"] == "a.txt"
    assert media_item({"type": 1, "text_item": {"text": "hi"}}) is None
    print("→ 媒体子项提取(image_item/file_item,文本项跳过)✓")

    tmp = Path(tempfile.mkdtemp())
    path = save_media(plain, tmp / "inbound", "照片 01.png")
    assert path.exists() and path.read_bytes() == plain
    print("→ 附件落盘:", path.relative_to(tmp))

    # 桥里:一条带图片的消息应该把附件路径喂给 agent
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["relay"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()          # type: ignore[assignment]
    bridge.api = FakeApi()              # type: ignore[assignment]
    bridge.runner = StubRunner()        # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")   # 别污染真实 sessions.json

    original = B.download_media
    B.download_media = lambda item, cdn_base="", label="media": plain  # type: ignore[assignment]
    try:
        bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx",
                       "item_list": [{"type": 2, "image_item": {"media": {"full_url": "https://cdn.example/img"}}}]})
        # 附件下载现在在 worker 线程里跑,桩要撑到它结束再还原
        time.sleep(1.2)
        assert bridge.runner.calls and "附件已存:inbound" in bridge.runner.calls[0], bridge.runner.calls
        assert any("附件已存:inbound" in t for _, t in bridge.api.sent), bridge.api.sent
        print("→ 带图片的消息:附件被存进工作目录,路径喂给 agent ✓")
    finally:
        B.download_media = original     # type: ignore[assignment]
    print("\n媒体移植通过。")


if __name__ == "__main__":
    main()
