"""weixin_api.py — 微信 iLink bot 协议的 Python 移植(照 openclaw-weixin 的 HTTP 形状)。

端点(base=https://ilinkai.weixin.qq.com):
  POST ilink/bot/get_bot_qrcode?bot_type=3   → {qrcode, qrcode_img_content}
  GET  ilink/bot/get_qrcode_status?qrcode=…  → 长轮询:{status, bot_token, ilink_bot_id, baseurl, ilink_user_id}
  POST ilink/bot/getupdates                  → 长轮询收消息(get_updates_buf 游标)
  POST ilink/bot/sendmessage                 → 发消息(msg 包一个 WeixinMessage)
  GET/POST ilink/bot/getconfig               → typing_ticket
  POST ilink/bot/sendtyping                  → 输入状态
  POST ilink/bot/msg/notifystart|notifystop  → 起止通知

不需要应用注册、不需要公网地址:扫码登录拿到 bot_token 之后就是纯 HTTP 轮询。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
QR_FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_BOT_TYPE = "3"
CHANNEL_VERSION = "2.4.10"          # 与移植来源对齐,服务端会看 channel_version
ILINK_APP_ID = "bot"
STALE_TOKEN_ERRCODE = -4001
# 服务端把登录会话当短期会话:绑定后如果客户端没马上开始长轮询,接口会回这个。
SESSION_TIMEOUT_ERRCODE = -14         # openclaw-weixin 里 token 失效的码

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
# 能下载成文件的媒体项 → 落盘时用的名字前缀
DOWNLOADABLE_ITEMS = {ITEM_IMAGE: "image", ITEM_FILE: "file", ITEM_VIDEO: "video"}


def client_version(version: str = CHANNEL_VERSION) -> int:
    parts = [int(p) for p in version.split(".")[:3]] + [0, 0, 0]
    major, minor, patch = parts[:3]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def base_info(bot_agent: str = "Strata/dsh") -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": bot_agent[:256]}


def _uin() -> str:
    n = int.from_bytes(secrets.token_bytes(4), "big")
    return base64.b64encode(str(n).encode("utf-8")).decode("ascii")


def _headers(token: str | None = None, get: bool = False) -> dict[str, str]:
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(client_version()),
    }
    if not get:
        headers["Content-Type"] = "application/json"
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["X-WECHAT-UIN"] = _uin()
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def _request(method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float) -> str:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return raw


class WeixinApi:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str | None = None,
                 bot_agent: str = "Strata/dsh"):
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.bot_agent = bot_agent

    def post(self, endpoint: str, payload: dict, timeout: float = 15.0) -> dict:
        url = self.base_url + endpoint.lstrip("/")
        body = json.dumps({**payload, "base_info": base_info(self.bot_agent)}).encode("utf-8")
        raw = _request("POST", url, body, _headers(self.token), timeout)
        return json.loads(raw or "{}")

    def get(self, endpoint: str, timeout: float = 15.0) -> dict:
        url = self.base_url + endpoint.lstrip("/")
        raw = _request("GET", url, None, _headers(None, get=True), timeout)
        return json.loads(raw or "{}")

    # ── 登录 ────────────────────────────────────────────────────────────
    def fetch_qrcode(self, local_tokens: list[str], bot_type: str = DEFAULT_BOT_TYPE) -> dict:
        url = f"{QR_FIXED_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(bot_type)}"
        body = json.dumps({"local_token_list": local_tokens, "base_info": base_info(self.bot_agent)}).encode()
        raw = _request("POST", url, body, _headers(None, get=False), 20.0)
        return json.loads(raw)

    def poll_qrcode_status(self, qrcode: str, verify_code: str | None = None,
                           base_url: str = QR_FIXED_BASE_URL, timeout: float = 35.0) -> dict:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}"
        if verify_code:
            endpoint += f"&verify_code={urllib.parse.quote(verify_code)}"
        api = WeixinApi(base_url, None, self.bot_agent)
        try:
            return api.get(endpoint, timeout=timeout)
        except TimeoutError:
            return {"status": "wait"}          # 长轮询超时就是"还没扫",正常
        except urllib.error.URLError:
            return {"status": "wait"}

    # ── 收发 ────────────────────────────────────────────────────────────
    def get_updates(self, get_updates_buf: str, timeout: float = 35.0) -> dict:
        try:
            return self.post("ilink/bot/getupdates", {"get_updates_buf": get_updates_buf}, timeout)
        except TimeoutError:
            return {"ret": 0, "msgs": []}
        except urllib.error.URLError as e:
            return {"ret": -1, "errmsg": str(e)}

    def send_text(self, to_user_id: str, text: str, context_token: str | None = None,
                  run_id: str | None = None) -> dict:
        client_id = f"strata-dsh-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": 2,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        if run_id:
            msg["run_id"] = run_id
        return self.post("ilink/bot/sendmessage", {"msg": msg}, timeout=15.0)

    def get_upload_url(self, filekey: str, media_type: int, to_user_id: str, rawsize: int,
                       rawfilemd5: str, filesize: int, aeskey_hex: str,
                       no_need_thumb: bool = True) -> dict:
        return self.post("ilink/bot/getuploadurl", {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": no_need_thumb,
            "aeskey": aeskey_hex,
        }, timeout=15.0)

    def send_item(self, to_user_id: str, item: dict, context_token: str | None = None) -> dict:
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"strata-dsh-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": 2,
            "item_list": [item],
        }
        if context_token:
            msg["context_token"] = context_token
        return self.post("ilink/bot/sendmessage", {"msg": msg}, timeout=20.0)

    def get_config(self) -> dict:
        return self.post("ilink/bot/getconfig", {}, timeout=10.0)

    def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int = 1) -> dict:
        return self.post("ilink/bot/sendtyping",
                         {"ilink_user_id": ilink_user_id, "typing_ticket": typing_ticket, "status": status},
                         timeout=10.0)

    def notify_start(self) -> dict:
        return self.post("ilink/bot/msg/notifystart", {}, timeout=10.0)

    def notify_stop(self) -> dict:
        return self.post("ilink/bot/msg/notifystop", {}, timeout=10.0)


def _ref_text(ref: dict) -> str:
    inner = (ref.get("message_item") or {}).get("text_item") or {}
    text = inner.get("text") or ref.get("title") or ""
    svr = ref.get("svr_id")
    if not text and svr:
        return f"[引用消息 id={svr},内容没带过来]"
    return f"[引用]{text}"


def text_of(message: dict) -> str:
    parts = []
    for item in message.get("item_list") or []:
        ref = item.get("ref_msg")
        if isinstance(ref, dict):
            quoted = _ref_text(ref)
            if quoted:
                parts.append(quoted)
        if item.get("type") == ITEM_TEXT:
            t = (item.get("text_item") or {}).get("text")
            if t:
                parts.append(t)
        elif item.get("type") == ITEM_VOICE:
            t = (item.get("voice_item") or {}).get("text")
            if t:
                parts.append(f"[语音转文字] {t}")
    return "\n".join(parts).strip()


class AccountStore:
    """登录凭证 + 同步游标,存成一个 JSON。"""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def token(self) -> str | None:
        t = self.load().get("token")
        return t if isinstance(t, str) and t else None

    def base_url(self) -> str:
        return self.load().get("base_url") or DEFAULT_BASE_URL

    def sync_buf(self) -> str:
        return self.load().get("get_updates_buf") or ""

    def set_sync_buf(self, buf: str) -> None:
        data = self.load()
        data["get_updates_buf"] = buf
        self.save(data)

    def local_tokens(self) -> list[str]:
        data = self.load()
        return [data["token"]] if data.get("token") else []
