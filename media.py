"""media.py — 微信 CDN 媒体的下载与解密(AES-128-ECB / PKCS7),移植自 openclaw-weixin 的 cdn 模块。

两种 aes_key 编码都支持:
  - base64(16 个原始字节)         → 图片
  - base64(32 个十六进制 ASCII 字符) → 文件/语音/视频
另外 image_item 上有 `aeskey`(直接就是 16 字节 hex 字符串)。
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

CDN_BASE_URL_DEFAULT = "https://novac2c.cdn.weixin.qq.com/c2c"

# 与 weixin_api 里的类型常量保持一致
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE = 1, 2, 3

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - 环境里没有 pycryptodome 时给出清晰提示
    AES = None


def _require_aes() -> None:
    if AES is None:
        raise RuntimeError("没装 pycryptodome:pip install pycryptodome")


def aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS7,跟插件的 encryptAesEcb 一样。"""
    _require_aes()
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(_pkcs7(plaintext))


def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    _require_aes()
    cipher = AES.new(key, AES.MODE_ECB)
    raw = cipher.decrypt(ciphertext)
    return _unpkcs7(raw)


def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad] * pad)


def _unpkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad] * pad):
        return data      # 服务端偶尔给的是未填充的明文,别硬报错
    return data[:-pad]


def aes_padded_size(plaintext_size: int) -> int:
    """密文长度(上传时要报给 getuploadurl)。"""
    return ((plaintext_size + 1 + 15) // 16) * 16


def parse_aes_key(aes_key_b64: str, label: str = "media") -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise ValueError(f"{label}: aes_key 得解码成 16 字节或 32 位 hex,实际 {len(decoded)} 字节")


def aes_key_from_item(item: dict, label: str = "media") -> bytes | None:
    """优先 image_item.aeskey(hex),其次 media.aes_key(base64)。"""
    hex_key = item.get("aeskey")
    if isinstance(hex_key, str) and re.fullmatch(r"[0-9a-fA-F]{32}", hex_key):
        return bytes.fromhex(hex_key)
    media = item.get("media") or {}
    aes = media.get("aes_key")
    if isinstance(aes, str) and aes:
        return parse_aes_key(aes, label)
    return None


def build_download_url(param: str, cdn_base: str = CDN_BASE_URL_DEFAULT) -> str:
    return f"{cdn_base.rstrip('/')}/download?encrypted_query_param={urllib.parse.quote(param, safe='')}"


def build_upload_url(param: str, filekey: str, cdn_base: str = CDN_BASE_URL_DEFAULT) -> str:
    return (f"{cdn_base.rstrip('/')}/upload?encrypted_query_param={urllib.parse.quote(param, safe='')}"
            f"&filekey={urllib.parse.quote(filekey, safe='')}")


def post_cdn(url: str, ciphertext: bytes, timeout: float = 60.0) -> str:
    """把密文 POST 给 CDN,回 x-encrypted-param(下载用的加密查询参数)。"""
    req = urllib.request.Request(url, data=ciphertext, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"CDN 上传 {resp.status}:{resp.headers.get('x-error-message', '')}")
        param = resp.headers.get("x-encrypted-param")
        if not param:
            raise RuntimeError("CDN 上传响应里没有 x-encrypted-param")
        return param


def upload_media(path: Path, to_user_id: str, api, cdn_base: str = CDN_BASE_URL_DEFAULT,
                 media_type: int = UPLOAD_IMAGE, post: Callable[[str, bytes], str] = post_cdn,
                 max_bytes: int = 25 * 1024 * 1024) -> dict:
    """本地文件 → getuploadurl → AES-ECB 加密 → CDN POST → 返回发媒体消息需要的信息。"""
    _require_aes()
    plaintext = Path(path).read_bytes()
    if len(plaintext) > max_bytes:
        raise RuntimeError(f"文件太大:{len(plaintext)} 字节 > {max_bytes}")
    rawsize = len(plaintext)
    rawfilemd5 = md5_hex(plaintext)
    filesize = aes_padded_size(rawsize)
    filekey = secrets.token_hex(16)
    aeskey = secrets.token_bytes(16)

    resp = api.get_upload_url(filekey, media_type, to_user_id, rawsize, rawfilemd5, filesize,
                              aeskey.hex())
    full = (resp.get("upload_full_url") or "").strip()
    param = resp.get("upload_param")
    if not full and not param:
        raise RuntimeError(f"getuploadurl 没给上传地址:{resp}")

    url = full or build_upload_url(param, filekey, cdn_base)
    download_param = post(url, aes_ecb_encrypt(plaintext, aeskey))

    return {
        "filekey": filekey,
        "download_param": download_param,
        "aeskey_hex": aeskey.hex(),
        "aeskey_base64_hex": base64.b64encode(aeskey.hex().encode("ascii")).decode(),
        "size": rawsize,
        "size_ciphertext": filesize,
    }


def image_item(uploaded: dict) -> dict:
    return {"type": ITEM_IMAGE, "image_item": {
        "media": {"encrypt_query_param": uploaded["download_param"],
                  "aes_key": uploaded["aeskey_base64_hex"], "encrypt_type": 1},
        "mid_size": uploaded["size_ciphertext"]}}


def video_item(uploaded: dict) -> dict:
    return {"type": ITEM_VIDEO, "video_item": {
        "media": {"encrypt_query_param": uploaded["download_param"],
                  "aes_key": uploaded["aeskey_base64_hex"], "encrypt_type": 1},
        "video_size": uploaded["size_ciphertext"]}}


def file_item(uploaded: dict, file_name: str) -> dict:
    return {"type": ITEM_FILE, "file_item": {
        "media": {"encrypt_query_param": uploaded["download_param"],
                  "aes_key": uploaded["aeskey_base64_hex"], "encrypt_type": 1},
        "file_name": file_name, "len": str(uploaded["size"]), "md5": ""}}
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"CDN 下载 {resp.status}:{url}")
        return resp.read()


def media_item(item: dict) -> dict | None:
    """取出带媒体的子项(image_item / file_item / video_item / voice_item)。"""
    for key in ("image_item", "file_item", "video_item", "voice_item"):
        sub = item.get(key)
        if isinstance(sub, dict) and (sub.get("media") or sub.get("aeskey")):
            return sub
    if isinstance(item.get("media"), dict):
        return item
    return None


def fetch_bytes(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"CDN 下载 {resp.status}:{url}")
        return resp.read()


def download_media(item: dict, cdn_base: str = CDN_BASE_URL_DEFAULT,
                   fetch: Callable[[str], bytes] = fetch_bytes, label: str = "media") -> bytes:
    """下载并解密一个媒体子项。full_url 优先;没有就用 encrypt_query_param 拼 CDN 下载地址。"""
    media = item.get("media") or {}
    url = media.get("full_url")
    if not url:
        param = media.get("encrypt_query_param")
        if not param:
            raise RuntimeError(f"{label}: 没有 full_url 也没有 encrypt_query_param")
        url = build_download_url(param, cdn_base)
    data = fetch(url)
    key = aes_key_from_item(item, label)
    return aes_ecb_decrypt(data, key) if key else data


def save_media(data: bytes, directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", name, flags=re.UNICODE)[:80] or "media"
    path = directory / safe
    path.write_bytes(data)
    return path


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
