"""bridge.py — 微信(iLink bot)↔ 本机 dsh agent 的桥。

比飞书那条路简单得多:不用建应用、不用公网地址。
  1) `python bridge.py --login` → 出一个二维码,手机微信扫一下并确认授权
  2) 凭证自动存进 weixin-account.json
  3) `python bridge.py` → 长轮询收消息,每条指令交给本机 dsh headless 执行,结果发回微信

用法:
  python bridge.py --login          扫码登录(会写 weixin-qr.png,屏幕扫描即可)
  python bridge.py --status         看账号/连接状态
  python bridge.py                  常驻:收消息 → 本机执行 → 回复
  python bridge.py --dry-run "…"    不连微信,只在本机跑一遍执行链路
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402

paths.temp_dir()      # 系统 temp 不可写时退回包内 .tmp(测试和 harness 都要 mkdtemp)
from agent_runner import (  # noqa: E402
    DEFAULTS,
    AgentRunner,
    describe_event,
    engine_health,
    find_patch,
)
from weixin_api import (  # noqa: E402
    CDN_BASE_URL,
    DOWNLOADABLE_ITEMS,
    AccountStore,
    MESSAGE_TYPE_USER,
    SESSION_TIMEOUT_ERRCODE,
    STALE_TOKEN_ERRCODE,
    WeixinApi,
    text_of,
)
from media import (  # noqa: E402
    UPLOAD_FILE,
    UPLOAD_IMAGE,
    UPLOAD_VIDEO,
    download_media,
    file_item,
    image_item,
    media_item,
    save_media,
    upload_media,
    video_item,
)
from text_filter import filter_markdown  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

CONFIG_PATH = HERE / "wechat.config.json"
ACCOUNT_PATH = HERE / "weixin-account.json"
SESSIONS_PATH = HERE / "sessions.json"
QR_PATH = HERE / "weixin-qr.png"
TRANSCRIPT_NAME = "wechat-chat.md"
INBOX_NAME = "wechat-inbox.md"
LOG = logging.getLogger("wechat-bridge")

MAX_REPLY_CHARS = 1200
MAX_FAILURES = 5
RETRY_SLEEP = 2.0
BACKOFF_SLEEP = 30.0
STALE_PAUSE_MS = 300_000


CONFIG_EXAMPLE_PATH = HERE / "config.example.json"


def load_config(path: Path) -> dict[str, Any]:
    """配置文件缺失时用 config.example.json,再缺就全走默认(跨平台可跑)。"""
    for candidate in (path, CONFIG_EXAMPLE_PATH):
        if candidate.is_file():
            cfg = json.loads(candidate.read_text(encoding="utf-8"))
            break
    else:
        cfg = {}
    cfg.setdefault("workspace", str(paths.workspace()))
    cfg.setdefault("allow_from", [])
    cfg.setdefault("bot_agent", "Strata/dsh")
    cfg.setdefault("long_poll_timeout_s", 35)
    cfg.setdefault("confirm_first", False)
    cfg.setdefault("allow_mode_switch", False)
    cfg.setdefault("inbound_dir", "inbound")
    cfg.setdefault("cdn_base", CDN_BASE_URL)
    cfg.setdefault("send_deliverables", True)
    cfg.setdefault("auto_relogin", True)
    cfg.setdefault("shared_session", True)   # 微信接到桌面当前会话(同一条对话)
    cfg.setdefault("session_id", "")         # 显式指定 dsh 会话 id(空=自动检测)
    cfg.setdefault("chat_mode", True)      # 微信走对话档(聊天助手),不是编程 agent
    cfg.setdefault("chat_context", True)   # 对话档把最近对话 + 机器现状垫进指令
    cfg.setdefault("relay", False)           # true:不在本机跑 headless,把指令转给桌面对话
    cfg.setdefault("relay_ack", True)
    cfg.setdefault("log_file", "")
    cfg.setdefault("agent", {})
    return cfg


class SessionStore:
    """微信对端 user id ↔ dsh session id / 权限档位。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                LOG.warning("sessions.json 读坏了,重新开始")
                self.data = {}

    def get(self, peer: str) -> str | None:
        return self.data.get(peer, {}).get("session_id")

    def get_mode(self, peer: str) -> str | None:
        return self.data.get(peer, {}).get("mode")

    def set(self, peer: str, session_id: str) -> None:
        rec = self.data.get(peer, {})
        rec["session_id"] = session_id
        rec["updated_at"] = int(time.time())
        self.data[peer] = rec
        self._save()

    def set_mode(self, peer: str, mode: str) -> None:
        rec = self.data.get(peer, {})
        rec["mode"] = mode
        self.data[peer] = rec
        self._save()

    def reset(self, peer: str) -> None:
        rec = self.data.get(peer, {})
        rec.pop("session_id", None)
        self.data[peer] = rec
        self._save()

    def detach(self, peer: str) -> None:
        """开一个独立会话(不再接桌面当前会话)。"""
        rec = self.data.get(peer, {})
        rec.pop("session_id", None)
        rec["detached"] = True
        self.data[peer] = rec
        self._save()

    def attach(self, peer: str, session_id: str) -> None:
        rec = self.data.get(peer, {})
        rec["detached"] = False
        rec["session_id"] = session_id
        rec["updated_at"] = int(time.time())
        self.data[peer] = rec
        self._save()

    def is_detached(self, peer: str) -> bool:
        return bool(self.data.get(peer, {}).get("detached"))

    def reset_mode(self, peer: str) -> None:
        rec = self.data.get(peer, {})
        rec.pop("mode", None)
        self.data[peer] = rec
        self._save()

    def set_ctx(self, peer: str, context_token: str | None) -> None:
        if not context_token:
            return
        rec = self.data.get(peer, {})
        rec["context_token"] = context_token
        self.data[peer] = rec
        self._save()

    def ctx(self, peer: str) -> str | None:
        return self.data.get(peer, {}).get("context_token")

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def split_answer(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return ["（没有产出内容)"]
    if len(text) <= MAX_REPLY_CHARS:
        return [text]
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(para) > MAX_REPLY_CHARS:
            if buf:
                parts.append(buf)
                buf = ""
            for i in range(0, len(para), MAX_REPLY_CHARS):
                parts.append(para[i:i + MAX_REPLY_CHARS])
            continue
        if len(buf) + len(para) + 2 > MAX_REPLY_CHARS:
            parts.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        parts.append(buf)
    total = len(parts)
    return [f"({i + 1}/{total})\n{p}" for i, p in enumerate(parts)]


COMMAND_ALIASES = {"rely": "relay", "history": "transcript", "chat": "transcript",
                   "stop": "abort", "kill": "abort", "reset": "new", "fresh": "new",
                   "sessions": "session", "id": "whoami", "me": "whoami", "info": "status"}


def slash_command(text: str) -> str | None:
    first = text.split(" ", 1)[0].strip().lower()
    if not first.startswith("/") or len(first) <= 1:
        return None
    cmd = first[1:]
    return COMMAND_ALIASES.get(cmd, cmd)


CODING_PATCH = HERE / "patches" / "coding.patch.yml"
CHAT_PATCH = HERE / "patches" / "chat.patch.yml"
AGENT_PATCHES = {"chat": str(CHAT_PATCH), "code": str(CODING_PATCH)}


class WeChatBridge:
    def __init__(self, cfg: dict[str, Any], config_path: Path | None = None):
        self.cfg = cfg
        self.config_path = config_path or CONFIG_PATH
        self.store = AccountStore(ACCOUNT_PATH)
        self.sessions = SessionStore(SESSIONS_PATH)
        self.mode = "chat" if cfg.get("chat_mode", True) else "code"
        self.runner = AgentRunner(self.agent_cfg(self.mode))
        self.api = WeixinApi(self.store.base_url(), self.store.token(), cfg["bot_agent"])
        self.lock = threading.Lock()
        self.pending: dict[str, Any] = {}
        self.seen: dict[str, float] = {}
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.typing_ticket: str | None = None
        self.last_event_at: float | None = None

    def agent_cfg(self, mode: str) -> dict[str, Any]:
        """对话档:换人设 + 关掉编程工具,默认 read-only。编程档沿用 feishu.patch.yml。"""
        base = dict(self.cfg.get("agent") or {})
        base["extra_patch"] = AGENT_PATCHES.get(mode, AGENT_PATCHES["chat"])
        if mode == "chat":
            base["permission_mode"] = "read-only"
        elif not base.get("permission_mode"):
            base["permission_mode"] = "workspace-write"
        return base

    def apply_agent_mode(self, mode: str) -> None:
        self.mode = mode
        self.cfg["chat_mode"] = mode == "chat"
        self.runner = AgentRunner(self.agent_cfg(mode))

    def persist_agent_mode(self, mode: str) -> None:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            data["chat_mode"] = mode == "chat"
            agent = data.get("agent") or {}
            agent["extra_patch"] = AGENT_PATCHES[mode]
            if mode == "chat":
                agent["permission_mode"] = "read-only"
            data["agent"] = agent
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError) as e:
            LOG.warning("写 agent 档配置失败:%s", e)

    # ── 出站 ────────────────────────────────────────────────────────────
    def to_inbox(self, peer: str, text: str, context_token: str | None) -> None:
        """relay 模式:把这条指令写进 wechat-inbox.md 等桌面那侧处理。"""
        path = Path(self.cfg["workspace"]) / INBOX_NAME
        stamp = time.strftime("%H:%M:%S")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] 待处理({peer}): {text}\ncontext_token: {context_token or ''}\n")
        except OSError as e:
            LOG.debug("写待办失败:%s", e)

    def recent_transcript(self, lines: int = 20) -> str:
        path = Path(self.cfg["workspace"]) / TRANSCRIPT_NAME
        try:
            data = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return "还没有对话记录。"
        return "\n".join(data[-lines:])

    def transcript(self, peer: str, kind: str, text: str) -> None:
        """把手机这条对话写进 wechat-chat.md,桌面这边(包括我)能直接读到同一条记录。"""
        path = Path(self.cfg["workspace"]) / TRANSCRIPT_NAME
        stamp = time.strftime("%H:%M:%S")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {kind}({peer[-12:]}): {text}\n")
        except OSError as e:
            LOG.debug("写对话记录失败:%s", e)

    def reply(self, peer: str, text: str, context_token: str | None = None) -> None:
        self.transcript(peer, "微信→手机", text)
        for chunk in split_answer(filter_markdown(text)):
            try:
                resp = self.api.send_text(peer, chunk, context_token)
                if resp.get("ret") not in (0, None):
                    LOG.warning("sendmessage ret=%s errmsg=%s", resp.get("ret"), resp.get("errmsg"))
            except Exception as e:
                LOG.error("发微信失败: %s", e)

    def typing(self, peer: str, status: int) -> None:
        if not self.typing_ticket:
            return
        try:
            self.api.send_typing(peer, self.typing_ticket, status)
        except Exception as e:
            LOG.debug("sendtyping 失败: %s", e)

    def _collect_attachments(self, message: dict) -> list[str]:
        """把微信发来的图片/文件/视频下载解密后存进工作目录,让 agent 能直接读。"""
        workspace = Path(self.cfg["workspace"])
        directory = workspace / self.cfg.get("inbound_dir", "inbound")
        lines: list[str] = []
        for idx, item in enumerate(message.get("item_list") or []):
            candidates = [item]
            ref_item = (item.get("ref_msg") or {}).get("message_item")
            if isinstance(ref_item, dict):
                candidates.append(ref_item)
            for sub_item in candidates:
                kind = DOWNLOADABLE_ITEMS.get(sub_item.get("type"))
                if not kind:
                    continue
                sub = media_item(sub_item)
                if sub is None:
                    continue
                ext = {"image": ".png", "video": ".mp4"}.get(kind, ".bin")
                name = (sub_item.get("file_item") or {}).get("file_name") or f"{kind}-{int(time.time())}-{idx}{ext}"
                try:
                    data = download_media(sub, self.cfg.get("cdn_base", CDN_BASE_URL), label=kind)
                    path = save_media(data, directory, name)
                    rel = path.relative_to(workspace) if path.is_relative_to(workspace) else path
                    lines.append(f"[附件已存:{rel}({len(data)} 字节)]")
                except Exception as e:
                    LOG.warning("附件下载失败(%s):%s", kind, e)
                    lines.append(f"[附件下载失败:{kind}({e})]")
        return lines

    def send_file(self, peer: str, path: Path, caption: str = "", context_token: str | None = None) -> str:
        """把工作目录里的文件发到微信(图片/视频/普通附件按扩展名分流)。"""
        workspace = Path(self.cfg["workspace"])
        resolved = (path if path.is_absolute() else workspace / path).resolve()
        if not resolved.is_relative_to(workspace):
            raise RuntimeError("只能发工作目录里的文件")
        if not resolved.is_file():
            raise RuntimeError(f"文件不存在:{resolved}")

        ext = resolved.suffix.lower()
        if ext in IMAGE_EXT:
            uploaded = upload_media(resolved, peer, self.api, self.cfg["cdn_base"], UPLOAD_IMAGE,
                                    max_bytes=MAX_UPLOAD_BYTES)
            item = image_item(uploaded)
        elif ext in VIDEO_EXT:
            uploaded = upload_media(resolved, peer, self.api, self.cfg["cdn_base"], UPLOAD_VIDEO,
                                    max_bytes=MAX_UPLOAD_BYTES)
            item = video_item(uploaded)
        else:
            uploaded = upload_media(resolved, peer, self.api, self.cfg["cdn_base"], UPLOAD_FILE,
                                    max_bytes=MAX_UPLOAD_BYTES)
            item = file_item(uploaded, resolved.name)

        if caption:
            self.reply(peer, caption, context_token)
        resp = self.api.send_item(peer, item, context_token)
        if resp.get("ret") not in (0, None):
            LOG.warning("send_item ret=%s errmsg=%s", resp.get("ret"), resp.get("errmsg"))
        return f"{resolved.name}({uploaded['size']} 字节)"

    def _deliverables(self, peer: str, written: list[str], context_token: str | None) -> list[str]:
        """把 agent 这次真正写过的文件发回手机(最多 2 个)。桥自己的状态文件永远不发。"""
        if not self.cfg.get("send_deliverables", True):
            return []
        workspace = Path(self.cfg["workspace"])
        sent: list[str] = []
        for raw in written:
            if len(sent) >= 2:
                break
            p = (workspace / raw) if not Path(raw).is_absolute() else Path(raw)
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(workspace) or not resolved.is_file():
                continue
            if is_bridge_artifact(resolved, workspace):
                continue
            try:
                sent.append(self.send_file(peer, resolved, "", context_token))
            except Exception as e:
                LOG.warning("交付物发送失败(%s):%s", resolved.name, e)
        return sent

    # ── 入站 ────────────────────────────────────────────────────────────
    def handle(self, message: dict) -> None:
        """长轮询线程只负责去重 + 排队,附件和执行都在 worker 里跑。"""
        peer = message.get("from_user_id") or ""
        if not peer:
            return
        mid = str(message.get("message_id") or "")
        if mid and self._is_duplicate(mid):
            LOG.info("重复消息(服务端重投),跳过:%s", mid)
            return
        self.last_event_at = time.time()
        self.pool.submit(self._process, message)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        if message_id in self.seen:
            return True
        self.seen[message_id] = now
        if len(self.seen) > 500:
            for k in sorted(self.seen, key=lambda k: self.seen[k])[:100]:
                self.seen.pop(k, None)
        return False

    def _process(self, message: dict) -> None:
        peer = message.get("from_user_id") or ""
        text = text_of(message)
        context_token = message.get("context_token")
        LOG.info("微信 incoming: peer=%s text=%s", peer, text[:120])
        self.transcript(peer, "手机→微信", text)
        self.sessions.set_ctx(peer, context_token)

        attachments = self._collect_attachments(message)
        if attachments:
            text = (text + "\n" + "\n".join(attachments)).strip()
        if not text:
            self.reply(peer, "这条消息里没有文字也没有附件能读的东西;发一条文字指令试试。", context_token)
            return

        allowed = list(self.cfg.get("allow_from", []))
        if allowed and peer not in allowed:
            LOG.info("对端 %s 不在 allow_from,忽略", peer)
            self.reply(peer, "这台机器只服务白名单里的账号。先把你自己的 id 填进 allow_from。", context_token)
            return

        cmd = slash_command(text)
        if cmd == "transcript":
            self.reply(peer, self.recent_transcript(), context_token)
            return
        if cmd:
            self._command(peer, cmd, text, context_token)
            return

        if self.cfg.get("relay"):
            # 不在本机起 headless agent,把这条转到桌面对话(我在那边读 wechat-inbox.md 并回)
            self.to_inbox(peer, text, context_token)
            if self.cfg.get("relay_ack", True):
                self.reply(peer, "已转到桌面对话,那边会回你。", context_token)
            return

        if self.cfg.get("confirm_first"):
            self._purge_pending()
            self.pending[peer + "!confirm"] = {"text": text, "context_token": context_token,
                                               "created_at": time.time()}
            self.reply(peer, f"要执行:{text[:400]}\n回 /confirm 才真的跑(/cancel 放弃)。", context_token)
            return

        self.pool.submit(self._run_task, peer, text, context_token)

    def _purge_pending(self) -> None:
        now = time.time()
        for key in [k for k, v in self.pending.items()
                    if isinstance(v, dict) and now - v.get("created_at", 0) > 900]:
            self.pending.pop(key, None)

    def _chat_context(self, peer: str) -> str:
        """对话档:把手机上最近的对话和机器现状摘要垫进去,换会话也不丢上下文。"""
        if not self.cfg.get("chat_context", True):
            return ""
        lines: list[str] = []
        recent = self.recent_transcript(12).splitlines()
        if recent and recent[0] != "还没有对话记录。":
            lines.append("最近对话:\n" + "\n".join(recent))
        sid = os.environ.get("DSH_SESSION_ID") or ""
        activity = "还没有活动" if not self.last_event_at else f"{int(time.time() - self.last_event_at)} 秒前"
        lines.append(f"机器现状:档={self.mode},工作目录={self.cfg['workspace']},"
                     f"桌面会话={sid or '(未知)'},桥最后活动于 {activity}。"
                     f"(以上只是背景,别对用户复述它)")
        return "\n\n".join(lines)[:900]

    def _run_task(self, peer: str, text: str, context_token: str | None) -> None:
        cancel = threading.Event()
        self.pending[peer] = cancel
        self.typing(peer, 1)

        written: list[str] = []
        pending_write: list[str] = []

        def on_event(ev: dict) -> None:
            """只记 agent 真正写过的文件(write/edit),不用扫目录猜。"""
            t = ev.get("type")
            if t == "tool_call" and ev.get("tool") in ("write", "edit"):
                p = (ev.get("input") or {}).get("file_path")
                if p:
                    pending_write.append(str(p))
            elif t == "tool_result":
                if ev.get("status") in ("completed", "done", "ok"):
                    written.extend(pending_write)
                pending_write.clear()

        try:
            with self.lock:
                if cancel.is_set():
                    self.reply(peer, "已取消,没执行。", context_token)
                    return
                sid = self._session_for(peer)
                mode = None if self.mode == "chat" else self.sessions.get_mode(peer)
                task = f"{text}\n\n{self._chat_context(peer)}" if self.mode == "chat" else text
                res = self.runner.run(task, sid, self.cfg["workspace"], on_event, cancel=cancel,
                                      permission_mode=mode)
                if sid and not res.ok:
                    # 接不上已存的会话(preset / 换过 DSH_HOME / 记在别的目录)就退化成新会话再跑一次
                    LOG.info("会话 %s 接不上(%s),改用新会话", sid, (res.error or res.text or "")[:120])
                    retry = self.runner.run(task, None, self.cfg["workspace"], on_event, cancel=cancel,
                                            permission_mode=mode)
                    if retry.ok:
                        res = retry
                        self.sessions.detach(peer)   # detach 会清 session_id,所以要放在记 id 之前
                        note = "(接不上原来的 dsh 会话,这条开新会话继续)"
                    else:
                        note = ""
                else:
                    note = ""
        finally:
            self.pending.pop(peer, None)
            self.typing(peer, 2)

        if res.session_id:
            self.sessions.set(peer, res.session_id)
        if res.ok:
            self.reply(peer, res.text + (f"\n{note}" if note else ""), context_token)
            new_files = self._deliverables(peer, written, context_token)
            if new_files:
                self.reply(peer, "已把这次新写的文件发过去:" + "、".join(new_files), context_token)
        else:
            self.reply(peer, f"执行失败:{res.error or res.text or '未知错误'}", context_token)

    def _session_for(self, peer: str) -> str | None:
        """微信对端用哪个 dsh 会话。对话档不去接桌面那条(带 agent preset,headless 接不了),
        只在没记录时开微信自己的新会话;编程档才尝试 shared_session 接桌面当前会话。"""
        sid = self.sessions.get(peer)
        if sid:
            return sid
        if self.sessions.is_detached(peer) or self.mode == "chat":
            return None
        if self.cfg.get("shared_session", True):
            forced = self.cfg.get("session_id")
            sid = forced or detect_current_session(self.cfg["workspace"])
            if sid:
                LOG.info("微信对端 %s 接到会话 %s", peer, sid)
                self.sessions.attach(peer, sid)
                return sid
        return None

    def _command(self, peer: str, cmd: str, text: str, context_token: str | None) -> None:
        if cmd == "whoami":
            self.reply(peer, f"你的微信对端 id:{peer}\n填进 wechat.config.json 的 allow_from 就只认你。",
                       context_token)
        elif cmd == "new":
            self.sessions.detach(peer)
            self.reply(peer, "已开独立新会话(不再接桌面当前会话)。想接回来发 /session current。",
                       context_token)
        elif cmd == "session":
            arg = text.split(" ", 1)[1].strip() if " " in text else ""
            if arg.lower() == "current":
                sid = detect_current_session(self.cfg["workspace"])
                if not sid:
                    self.reply(peer, "没找到桌面的当前会话目录。", context_token)
                else:
                    self.sessions.attach(peer, sid)
                    self.reply(peer, f"已接到桌面当前会话:{sid}", context_token)
            elif arg:
                self.sessions.attach(peer, arg)
                self.reply(peer, f"已用会话:{arg}", context_token)
            else:
                sid = self.sessions.get(peer)
                shared = self.cfg.get("shared_session", True) and not self.sessions.is_detached(peer)
                self.reply(peer, f"当前会话:{sid or '(还没有)'}\n"
                                 f"{'接的是桌面当前对话' if shared else '独立会话'}\n"
                                 f"/session current 接桌面 · /new 开独立会话", context_token)
        elif cmd == "abort":
            ev = self.pending.get(peer)
            self.reply(peer, "已请求取消。" if ev else "当前没有正在跑的任务。", context_token)
            if ev:
                ev.set()
        elif cmd == "status":
            acc = self.store.load()
            sid = self.sessions.get(peer) or "(还没有会话)"
            try:
                health = engine_health(self.runner.cfg["serve_url"], self.runner.api_key())
                model = health.get("models") if health["ok"] else health.get("error")
            except Exception as e:
                model = e
            mode = self.sessions.get_mode(peer) or self.runner.cfg.get("permission_mode")
            shared = self.cfg.get("shared_session", True) and not self.sessions.is_detached(peer)
            self.reply(
                peer,
                f"账号:{acc.get('ilink_bot_id') or '(未登录)'}\n会话:{sid}\n"
                f"会话来源:{'桌面当前对话(共享上下文)' if shared else '微信独立会话'}\n"
                f"档:{'对话(chat,read-only)' if self.mode == 'chat' else '编程(code)'}\n"
                f"本机模型:{model}\n工作目录:{self.cfg['workspace']}\n档位:{mode}\n"
                f"最后活动:{int(time.time() - (self.last_event_at or 0))}s 前",
                context_token,
            )
        elif cmd == "mode":
            arg = text.split(" ", 1)[1].strip().lower() if " " in text else ""
            allowed = list(self.cfg.get("allow_from", []))
            modes = ("read-only", "workspace-write", "danger-full-access")
            if self.mode == "chat":
                self.reply(peer, "对话档固定 read-only(不会动机器)。想改档位先 /agent code。", context_token)
            elif not self.cfg.get("allow_mode_switch"):
                self.reply(peer, "档位切换没开(配置 allow_mode_switch: true)。", context_token)
            elif allowed and peer not in allowed:
                self.reply(peer, "你不在白名单里,不能切档位。", context_token)
            elif arg == "default":
                self.sessions.reset_mode(peer)
                self.reply(peer, f"回到默认档位:{self.runner.cfg.get('permission_mode')}", context_token)
            elif arg in modes:
                self.sessions.set_mode(peer, arg)
                self.reply(peer, f"这条会话档位改成 {arg}", context_token)
            else:
                self.reply(peer, "档位:" + " / ".join(modes) + " / default", context_token)
        elif cmd == "confirm":
            plan = self.pending.pop(peer + "!confirm", None)
            if not isinstance(plan, dict):
                self.reply(peer, "没有待确认的指令;直接发指令就行。", context_token)
                return
            self.pool.submit(self._run_task, peer, plan["text"], plan.get("context_token"))
        elif cmd == "cancel":
            plan = self.pending.pop(peer + "!confirm", None)
            self.reply(peer, "已放弃。" if plan else "没有待确认的指令。", context_token)
        elif cmd == "file":
            arg = text.split(" ", 1)[1].strip() if " " in text else ""
            if not arg:
                self.reply(peer, "用法:/file <工作目录里的相对路径>", context_token)
                return
            try:
                name = self.send_file(peer, Path(arg), "", context_token)
                self.reply(peer, f"已发送:{name}", context_token)
            except Exception as e:
                self.reply(peer, f"发送失败:{e}", context_token)
        elif cmd == "agent":
            arg = text.split(" ", 1)[1].strip().lower() if " " in text else ""
            if arg in ("chat", "code"):
                self.apply_agent_mode(arg)
                self.persist_agent_mode(arg)
                self.reply(peer, "已切到对话档:聊天人设,编程工具都关掉(read-only),不会动机器上的东西。"
                                 if arg == "chat" else
                                 "已切到编程档:可以用工具写文件、跑命令(workspace-write)。", context_token)
            else:
                self.reply(peer, f"当前档:{'对话(chat)' if self.mode == 'chat' else '编程(code)'}\n"
                                 f"/agent chat 聊天 · /agent code 用工具干活", context_token)
        elif cmd == "relay":
            arg = text.split(" ", 1)[1].strip().lower() if " " in text else ""
            if arg in ("on", "off"):
                on = arg == "on"
                self.cfg["relay"] = on
                try:
                    data = json.loads(self.config_path.read_text(encoding="utf-8"))
                    data["relay"] = on
                    self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
                except (OSError, json.JSONDecodeError) as e:
                    LOG.warning("写 relay 配置失败:%s", e)
                self.reply(peer, "已切到 relay 模式:指令写进 wechat-inbox.md,由桌面对话回答。"
                                 if on else "已切回本机 agent 执行(快回模式)。", context_token)
            else:
                self.reply(peer, f"relay 现在:{'开' if self.cfg.get('relay') else '关'}\n"
                                 f"/relay on 让桌面对话回答 · /relay off 回到本机 agent", context_token)
        elif cmd == "help":
            self.reply(peer, "指令:/status · /whoami · /agent chat|code · /session · /session current · "
                             "/transcript · /relay on|off · /new · /abort · /mode <档位> · /file <路径> · "
                             "/confirm · /cancel · /help\n"
                             "默认是对话档(聊天,不动机器);/agent code 才用工具干活。", context_token)
        else:
            self.reply(peer, f"未知指令 {text.split(' ', 1)[0]};可用:"
                             "/status · /whoami · /session · /transcript · /relay on|off · /new · /abort · "
                             "/mode <档位> · /file <路径> · /confirm · /cancel · /help", context_token)

    # ── 常驻长轮询 ──────────────────────────────────────────────────────
    def run_forever(self) -> None:
        token = self.store.token()
        if not token:
            print("还没登录。先跑: python bridge.py --login", file=sys.stderr)
            raise SystemExit(2)
        if isinstance(self.api, WeixinApi):
            self.api.token = token
            self.api.base_url = self.store.base_url().rstrip("/") + "/"
        try:
            cfg_resp = self.api.get_config()
            self.typing_ticket = cfg_resp.get("typing_ticket")
        except Exception as e:
            LOG.warning("getConfig 失败(typing 提示不可用):%s", e)
        self.api.notify_start()
        LOG.info("已连微信(%s),等你在手机上发消息。Ctrl+C 退出。", self.store.base_url())

        buf = self.store.sync_buf()
        timeout = float(self.cfg["long_poll_timeout_s"])
        failures = 0
        try:
            while True:
                resp = self.api.get_updates(buf, timeout)
                ret, errcode = resp.get("ret"), resp.get("errcode")
                if (ret not in (0, None)) or (errcode not in (0, None)):
                    if errcode in (STALE_TOKEN_ERRCODE, SESSION_TIMEOUT_ERRCODE) or ret in (
                        STALE_TOKEN_ERRCODE,
                        SESSION_TIMEOUT_ERRCODE,
                    ):
                        LOG.error("登录凭证/会话失效(errcode=%s, errmsg=%s)", errcode or ret, resp.get("errmsg"))
                        if self.cfg.get("auto_relogin", True):
                            LOG.info("自动重新扫码:二维码写在 %s,链接写在 weixin-qr-link.txt", QR_PATH)
                            acc = self.login(QR_PATH)
                            if acc:
                                try:
                                    self.api.token = acc["token"]
                                    self.api.base_url = acc["base_url"].rstrip("/") + "/"
                                except Exception as e:
                                    LOG.warning("换 token 失败:%s", e)
                                continue
                            LOG.error("自动重新登录没成,退避 5 分钟后再试")
                        time.sleep(STALE_PAUSE_MS / 1000)
                        continue
                    failures += 1
                    LOG.warning("getUpdates ret=%s errcode=%s errmsg=%s (%s/%s)",
                                ret, errcode, resp.get("errmsg"), failures, MAX_FAILURES)
                    time.sleep(BACKOFF_SLEEP if failures >= MAX_FAILURES else RETRY_SLEEP)
                    if failures >= MAX_FAILURES:
                        failures = 0
                    continue
                failures = 0
                new_buf = resp.get("get_updates_buf")
                if new_buf:
                    buf = new_buf
                    self.store.set_sync_buf(new_buf)
                for msg in resp.get("msgs") or []:
                    if msg.get("message_type") not in (None, MESSAGE_TYPE_USER):
                        continue
                    self.handle(msg)
        finally:
            try:
                self.api.notify_stop()
            except Exception as e:
                LOG.debug("notifystop 失败: %s", e)

    # ── 扫码登录 ────────────────────────────────────────────────────────
    def login(self, qr_out: Path | None = None, attempts: int = 5) -> dict[str, Any]:
        """扫码登录。二维码过期会自动换一张新码(最多 attempts 次)。"""
        base = self.api.base_url.rstrip("/")
        for attempt in range(1, attempts + 1):
            acc = self._login_once(base, qr_out, attempt)
            if acc:
                return acc
        return {}

    def _login_once(self, base: str, qr_out: Path | None, attempt: int) -> dict[str, Any]:
        qr = self.api.fetch_qrcode(self.store.local_tokens())
        qrcode = qr.get("qrcode")
        img_url = qr.get("qrcode_img_content")
        if not qrcode:
            raise RuntimeError(f"拿不到二维码:{qr}")

        print(f"[第 {attempt} 张二维码] 扫码链接(手机浏览器打开也行):\n  {img_url}", flush=True)
        png = self.render_qr(img_url, qr_out or QR_PATH)
        if png:
            print(f"二维码图片:{png}(屏幕上用微信扫这张;服务端大约 2 分钟过期,过期会自动换下一张)", flush=True)
        # 把当前链接写进文件,方便别人/别轮次直接读到最新链接
        try:
            (QR_PATH.parent / "weixin-qr-link.txt").write_text(
                f"{img_url}\n生成:{time.strftime('%H:%M:%S')}(约 2 分钟后过期,第 {attempt} 张)\n",
                encoding="utf-8")
        except OSError as e:
            LOG.debug("写链接文件失败:%s", e)

        verify_code = None
        deadline = time.time() + 5 * 60
        while time.time() < deadline:
            st = self.api.poll_qrcode_status(qrcode, verify_code, base_url=base, timeout=35)
            status = st.get("status")
            LOG.info("QR status: %s", status)
            if status == "wait":
                continue
            if status == "scaned":
                print("已扫码,请在手机上点确认授权…")
                continue
            if status == "need_verifycode":
                verify_code = input("输入微信给出的验证码(回车确认): ").strip() or None
                continue
            if status == "scaned_but_redirect":
                host = st.get("redirect_host")
                if host:
                    base = f"https://{host}"
                    LOG.info("切到 IDC 主机 %s", host)
                continue
            if status == "verify_code_blocked":
                print("验证码被拒,重新扫码再来一次。")
                return {}
            if status == "expired":
                print("二维码过期了,重新跑 --login。")
                return {}
            if status == "confirmed" or st.get("bot_token"):
                acc = {
                    "token": st.get("bot_token"),
                    "base_url": st.get("baseurl") or base,
                    "ilink_bot_id": st.get("ilink_bot_id"),
                    "ilink_user_id": st.get("ilink_user_id"),
                    "logged_in_at": int(time.time()),
                }
                self.store.save(acc)
                if isinstance(self.api, WeixinApi):
                    self.api.token = acc["token"]
                    self.api.base_url = acc["base_url"].rstrip("/") + "/"
                print("登录完成,凭证已存进", ACCOUNT_PATH)
                print(f"你的微信对端 id:{acc.get('ilink_user_id')} —— 填进 allow_from。")
                return acc
        print("等超时了,重新跑 --login。")
        return {}

    @staticmethod
    def render_qr(url: str, path: Path) -> Path | None:
        try:
            import qrcode
        except ImportError:
            print("没装 qrcode 库( pip install 'qrcode[pil]' ),只能用上面的链接。")
            return None
        img = qrcode.make(url)
        img.save(path)
        return path


SKIP_DIR_NAMES = {"wechat-bridge", "feishu-bridge", "weixin-probe", "inbound",
                  "__pycache__", ".venv", ".git", "node_modules"}
SKIP_FILE_NAMES = {"sessions.json", "weixin-account.json", "wechat.config.json",
                   "feishu.config.json", "weixin-qr.png", "weixin-qr-link.txt",
                   "config.example.json", "requirements.txt", ".gitignore",
                   TRANSCRIPT_NAME, INBOX_NAME}


def is_bridge_artifact(path: Path, workspace: Path) -> bool:
    """桥自己的状态/配置文件,不是 agent 的交付物。"""
    try:
        rel = path.relative_to(workspace)
    except ValueError:
        return True
    parts = [p.lower() for p in rel.parts]
    if any(p in SKIP_DIR_NAMES for p in parts[:-1]):
        return True
    name = rel.name
    return name in SKIP_FILE_NAMES or name.endswith(".log") or name.startswith("weixin-qr")


def session_slug(workspace: str) -> str:
    """dsh 把工作目录路径转成 sessions 下的目录名(C:\\a\\b -> --C-a-b--)。"""
    core = re.sub(r"[^A-Za-z0-9]+", "-", workspace).strip("-")
    return f"--{core}--"


def detect_current_session(workspace: str) -> str | None:
    """桌面当前对话的 dsh 会话 id。优先用环境变量 DSH_SESSION_ID(最准),
    拿不到才退化成"这个工作目录里最近活动的会话目录"。"""
    sid = (os.environ.get("DSH_SESSION_ID") or "").strip()
    if sid:
        return sid
    home = os.environ.get("DSH_HOME") or DEFAULTS["dsh_home"]
    d = Path(home) / "sessions" / session_slug(workspace)
    try:
        entries = [p for p in d.iterdir() if p.is_dir() and p.name.startswith("session-")]
    except OSError as e:
        LOG.debug("读不到会话目录 %s:%s", d, e)
        return None
    if not entries:
        return None
    newest = max(entries, key=lambda p: p.stat().st_mtime)
    return newest.name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--login", action="store_true", help="扫码登录微信")
    ap.add_argument("--status", action="store_true", help="看账号与本机模型状态")
    ap.add_argument("--dry-run", nargs="?", const="列出工作目录里的文件", metavar="TASK",
                    help="不连微信,只跑一遍本机执行链路")
    ap.add_argument("--and-serve", action="store_true", help="扫码登录成功后立刻开始收消息(登录会话有效期很短)")
    ap.add_argument("--doctor", action="store_true", help="换机器时先跑:逐项检查这台机器能不能跑起来")
    ap.add_argument("--qr-link", action="store_true", help="只打印一张新二维码的链接(手机浏览器打开),不等扫码")
    ap.add_argument("--qr-out", default=str(QR_PATH))
    ap.add_argument("--selftest", nargs="?", const="在工作目录写 selftest-交付物.txt,内容写 hello",
                    metavar="TASK", help="跑一次真实 agent,打印这次会回传哪些文件(不连微信、不发送)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(Path(args.config))
    if cfg.get("log_file"):
        handler = logging.FileHandler(cfg["log_file"], encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)

    bridge = WeChatBridge(cfg)

    if args.doctor:
        import doctor
        for line in doctor.run_checks():
            print(line)
        return

    if args.qr_link:
        qr = bridge.api.fetch_qrcode(bridge.store.local_tokens())
        print(qr.get("qrcode_img_content") or qr.get("qrcode") or "")
        return

    if args.login:
        acc = bridge.login(Path(args.qr_out))
        if not acc:
            raise SystemExit(1)
        if args.and_serve:
            # 登录会话有效期很短,绑定后必须立刻开始长轮询,否则服务端回 session timeout
            bridge.run_forever()
        raise SystemExit(0)

    if args.status:
        acc = bridge.store.load()
        print(f"账号:{acc.get('ilink_bot_id') or '(未登录)'}")
        print(f"对端 id:{acc.get('ilink_user_id') or '?'}")
        print(f"baseUrl:{acc.get('base_url') or '(默认)'}")
        print(f"token:{'none' if not acc.get('token') else 'present'}")
        print(f"sync buf:{len(bridge.store.sync_buf())} 字节")
        try:
            health = engine_health(bridge.runner.cfg["serve_url"], bridge.runner.api_key())
            print(f"本机模型:{health.get('models') if health['ok'] else health.get('error')}")
        except Exception as e:
            print(f"本机模型:拿不到密钥({e})")
        patch = bridge.runner.cfg.get("patch") or find_patch(bridge.runner.cfg["patch_dir"])
        print(f"profile patch:{patch or '(没找到)'}")
        print(f"会话映射:{len(bridge.sessions.data)} 个对端已绑定")
        if cfg.get("shared_session", True):
            print(f"共享会话:{detect_current_session(cfg['workspace']) or '(没检测到桌面当前会话)'}")
        print("环境(跨平台默认,可用环境变量覆盖):")
        for k, v in paths.describe().items():
            print(f"  {k}:{v}")
        return

    if args.dry_run is not None:
        sid = bridge.sessions.get("dry-run")
        print(f"[dry-run] 任务:{args.dry_run}\n[dry-run] 会话:{sid or '(新)'}")
        res = bridge.runner.run(args.dry_run, sid, cfg["workspace"], None)
        if res.session_id:
            bridge.sessions.set("dry-run", res.session_id)
        print(f"[退出码 {res.exit_code}] [会话 {res.session_id}]\n{res.text}")
        return

    if args.selftest is not None:
        # 不连微信:跑一次真实 agent,只打印"这次会回传哪些文件"(验证交付物判定)
        sid = cfg.get("session_id") or None   # selftest 用新会话,避免去接桌面那条带 preset 的会话
        written: list[str] = []
        pending: list[str] = []

        def on_event(ev: dict) -> None:
            t = ev.get("type")
            if t == "tool_call" and ev.get("tool") in ("write", "edit"):
                p = (ev.get("input") or {}).get("file_path")
                if p:
                    pending.append(str(p))
            elif t == "tool_result":
                if ev.get("status") in ("completed", "done", "ok"):
                    written.extend(pending)
                pending.clear()

        print(f"[selftest] 会话:{sid or '(新)'}\n[selftest] 任务:{args.selftest}")
        res = bridge.runner.run(args.selftest, sid, cfg["workspace"], on_event)
        print(f"[退出码 {res.exit_code}] [会话 {res.session_id}]")
        print(f"[selftest] agent 写过的文件:{written or '(无)'}")
        workspace = Path(cfg["workspace"])
        would = []
        for raw in written:
            p = (workspace / raw) if not Path(raw).is_absolute() else Path(raw)
            try:
                r = p.resolve()
            except OSError:
                continue
            if r.is_relative_to(workspace) and r.is_file() and not is_bridge_artifact(r, workspace):
                would.append(r.name)
        print(f"[selftest] 会回传:{would or '(无)'}")
        print(f"[selftest] 文本:{res.text[:400]}")
        return

    bridge.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已退出。", file=sys.stderr)
