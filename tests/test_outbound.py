"""test_outbound.py — 验证出站媒体(上传→CDN→发微信)的移植,全部离线用桩。

跑法: ..\\feishu-bridge\\.venv\\Scripts\\python.exe test_outbound.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import base64
import tempfile
import time
from pathlib import Path

import bridge as B
from agent_runner import RunResult
from media import aes_ecb_decrypt, file_item, image_item, upload_media


class StubApi:
    def __init__(self) -> None:
        self.upload_reqs: list[dict] = []
        self.items: list[dict] = []
        self.texts: list[str] = []

    def get_upload_url(self, filekey, media_type, to_user_id, rawsize, rawfilemd5, filesize,
                       aeskey_hex, no_need_thumb=True) -> dict:
        self.upload_reqs.append({"filekey": filekey, "media_type": media_type, "to_user_id": to_user_id,
                                 "rawsize": rawsize, "rawfilemd5": rawfilemd5, "filesize": filesize,
                                 "aeskey": aeskey_hex, "no_need_thumb": no_need_thumb})
        return {"ret": 0, "upload_param": "up-param-1"}

    def send_item(self, to_user_id, item, context_token=None) -> dict:
        self.items.append(item)
        return {"ret": 0, "message_id": "m-item"}

    def send_text(self, to_user_id, text, context_token=None, run_id=None) -> dict:
        self.texts.append(text)
        return {"ret": 0, "message_id": "m-text"}

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
        (Path(cwd) / "报告.txt").write_text("新写的交付物", encoding="utf-8")
        (Path(cwd) / "没写成.txt").write_text("失败的那次不该发", encoding="utf-8")
        if on_event:
            # 照真实 NDJSON:tool_call 带 file_path,tool_result 带 status
            on_event({"type": "tool_call", "tool": "write",
                      "input": {"file_path": "报告.txt", "content": "x"}})
            on_event({"type": "tool_result", "status": "completed"})
            on_event({"type": "tool_call", "tool": "edit",
                      "input": {"file_path": "没写成.txt", "old_string": "a", "new_string": "b"}})
            on_event({"type": "tool_result", "status": "failed"})
            on_event({"type": "tool_call", "tool": "write",
                      "input": {"file_path": "sessions.json", "content": "{}"}})
            on_event({"type": "tool_result", "status": "completed"})
        return RunResult(ok=True, text="写好了报告.txt", session_id="s-1", exit_code=0)


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    img = tmp / "图.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" * 4)
    api = StubApi()

    posted: list[tuple[str, bytes]] = []

    def fake_post(url: str, ciphertext: bytes) -> str:
        posted.append((url, ciphertext))
        return "download-param-1"

    uploaded = upload_media(img, "wx-me", api, "https://cdn.test/c2c", 1, post=fake_post)
    assert api.upload_reqs[0]["media_type"] == 1 and api.upload_reqs[0]["rawsize"] == img.stat().st_size
    assert api.upload_reqs[0]["filesize"] == uploaded["size_ciphertext"]
    assert posted[0][0] == "https://cdn.test/c2c/upload?encrypted_query_param=up-param-1&filekey=" + uploaded["filekey"]
    key = bytes.fromhex(uploaded["aeskey_hex"])
    assert aes_ecb_decrypt(posted[0][1], key) == img.read_bytes(), "CDN 上传的密文解不回原文"
    assert base64.b64decode(uploaded["aeskey_base64_hex"]).decode() == uploaded["aeskey_hex"]
    print("→ 上传:getuploadurl 参数、CDN 地址、AES-ECB 密文、密钥两种形态都对 ✓")

    item = image_item(uploaded)
    assert item["type"] == 2 and item["image_item"]["media"]["encrypt_query_param"] == "download-param-1"
    assert item["image_item"]["media"]["aes_key"] == uploaded["aeskey_base64_hex"]
    assert item["image_item"]["mid_size"] == uploaded["size_ciphertext"]
    fitem = file_item(uploaded, "报告.txt")
    assert fitem["type"] == 4 and fitem["file_item"]["file_name"] == "报告.txt"
    assert fitem["file_item"]["len"] == str(uploaded["size"])
    print("→ image_item / file_item 形状正确 ✓")

    # 桥:发工作目录里的文件;目录外的要拒绝
    cfg = B.load_config(B.CONFIG_PATH)
    cfg["relay"] = False
    cfg["chat_mode"] = False
    cfg["chat_context"] = False
    cfg["workspace"] = str(tmp)
    cfg["allow_from"] = []
    cfg["send_deliverables"] = True
    bridge = B.WeChatBridge(cfg)
    bridge.store = FakeStore()          # type: ignore[assignment]
    bridge.api = api                    # type: ignore[assignment]
    bridge.runner = StubRunner()        # type: ignore[assignment]
    bridge.sessions = B.SessionStore(tmp / "sessions.json")   # 别污染真实 sessions.json
    B.upload_media = lambda path, to, a, cdn="", mt=1, post=None, max_bytes=0: uploaded  # type: ignore[assignment]
    try:
        name = bridge.send_file("wx-me", Path("图.png"), "给你张图")
        assert "图.png" in name
        assert api.items and api.texts, "没发出媒体或说明文本"
        try:
            bridge.send_file("wx-me", Path("../outside.txt"))
            raise AssertionError("目录外的文件竟然被发送了")
        except RuntimeError as e:
            assert "工作目录" in str(e)
        print("→ /file 发送正常,工作目录外的路径被拒绝 ✓")

        # 跑一条指令,新写的文件应该自动回传
        api.items.clear()
        api.texts.clear()
        bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx",
                       "item_list": [{"type": 1, "text_item": {"text": "写个报告"}}]})
        time.sleep(1.5)
        assert bridge.runner.calls == ["写个报告"]
        names = [i.get("file_item", {}).get("file_name") for i in api.items]
        assert names == ["报告.txt"], names
        text = " ".join(api.texts)
        assert "已把这次新写的文件发过去:报告.txt" in text, api.texts
        assert "sessions.json" not in text and "没写成" not in text, api.texts
        print("→ 只回传 agent 真正写成的文件;桥自己的 sessions.json 和失败的那次都不发 ✓")

        # /file 指令
        api.items.clear()
        api.texts.clear()
        bridge.handle({"from_user_id": "wx-me", "message_type": 1, "context_token": "ctx",
                       "item_list": [{"type": 1, "text_item": {"text": "/file 报告.txt"}}]})
        time.sleep(0.8)
        assert api.items and any("已发送" in t for t in api.texts), (api.items, api.texts)
        print("→ /file 指令发文件 ✓")
    finally:
        import media as M
        B.upload_media = M.upload_media   # type: ignore[assignment]

    print("\n出站媒体移植通过。")


if __name__ == "__main__":
    main()
