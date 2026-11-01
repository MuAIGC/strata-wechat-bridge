"""agent_runner.py — 把一条文本指令交给本机 dsh(headless)执行,并把过程事件流出来。

设计要点:
- 子进程 stdout/stderr 都写到临时文件,而不是管道。这样在受限沙箱里也能跑
  (沙箱里程序不能开命名管道),在普通终端里同样有效。
- 事件是换行分隔的 JSON(`dsh --profile headless --json`),边跑边读,
  所以飞书那边能拿到"正在执行 xxx"的进度。
- 模型走本机 Strata 服务(:18500),密钥从 native-instances 里读并逐个校验。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from paths import dsh_home, harness_bin, key_dir, key_env, node_bin, patch_dir, serve_url, temp_dir

HERE = Path(__file__).resolve().parent

DEFAULTS: dict[str, Any] = {
    "node": node_bin(),
    "harness_bin": harness_bin(),
    "profile": "headless",
    "patch": "",  # 空 = 自动找最新的 profile-*.patch.yml
    "extra_patch": str(HERE / "patches" / "coding.patch.yml"),
    "dsh_home": str(dsh_home()),
    "patch_dir": str(patch_dir()),
    "key_dir": str(key_dir()),
    "serve_url": serve_url(),
    "key_env": key_env(),
    "permission_mode": "workspace-write",  # 提权请求会被自动拒绝;danger-full-access 才允许本机任意操作
    "timeout_s": 900,
    "tmp_dir": str(temp_dir()),
}


def _newest(dir_path: Path, glob: str) -> Path | None:
    if not dir_path.is_dir():
        return None
    files = [p for p in dir_path.glob(glob) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def find_patch(patch_dir: str) -> str:
    p = _newest(Path(patch_dir), "profile-*.patch.yml")
    return str(p) if p else ""


def resolve_api_key(key_dir: str, serve_url: str, timeout: float = 6.0) -> tuple[str, str]:
    """返回 (api_key, 来源文件)。逐个候选密钥试一次 /v1/models,取第一个能用的。"""
    d = Path(key_dir)
    candidates = sorted(
        (p for p in d.glob("*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    errors: list[str] = []
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{p.name}: 读取失败 {e}")
            continue
        key = data.get("api_key")
        if not isinstance(key, str) or not key:
            continue
        req = urllib.request.Request(serve_url, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return key, p.name
                errors.append(f"{p.name}: HTTP {resp.status}")
        except Exception as e:
            errors.append(f"{p.name}: {e}")
    raise RuntimeError(
        "本机推理服务没有可用的密钥(" + serve_url + ")。"
        "确认 Strata 正在 serving;或把 api_key 直接写进配置。候选: " + "; ".join(errors)
    )


@dataclass
class RunResult:
    ok: bool
    text: str
    session_id: str | None
    exit_code: int
    events: list[dict] = field(default_factory=list)
    stderr_tail: str = ""
    error: str | None = None


class AgentRunner:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = dict(DEFAULTS)
        cfg.update(config or {})
        self.cfg = cfg
        self._key_cache: tuple[str, str] | None = None

    def api_key(self) -> str:
        inline = self.cfg.get("api_key")
        if inline:
            return str(inline)
        if self._key_cache is None:
            self._key_cache = resolve_api_key(self.cfg["key_dir"], self.cfg["serve_url"])
        return self._key_cache[0]

    def build_command(self, task: str, session_id: str | None) -> list[str]:
        hb = self.cfg.get("harness_bin") or ""
        if not hb or not Path(hb).is_file():
            raise RuntimeError(
                "没找到 deepseek-harness 的 lib/bin.js。装好 Strata/dsh 后设环境变量 "
                "DSH_HARNESS_BIN(或在 config.json 的 agent.harness_bin 里写路径)。")
        patch = self.cfg.get("patch") or find_patch(self.cfg["patch_dir"])
        extra = self.cfg.get("extra_patch") or ""
        cmd = [self.cfg["node"], hb, "--profile", self.cfg["profile"]]
        if patch:
            cmd += ["--patch", patch]
        if extra and Path(extra).is_file():
            cmd += ["--patch", extra]
        if session_id:
            cmd += ["--session-id", session_id]
        cmd += ["--json", task]
        return cmd

    def build_env(self, permission_mode: str | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["DSH_HOME"] = self.cfg["dsh_home"]
        env[self.cfg["key_env"]] = self.api_key()
        mode = permission_mode or self.cfg.get("permission_mode")
        if mode:
            env["DSH_PERMISSION_MODE"] = mode
        # 沙箱里 %TEMP% 可能不可写(spill-local 要 mkdtemp);指到一个能写的目录。
        tmp_dir = self.cfg.get("tmp_dir")
        if tmp_dir:
            Path(tmp_dir).mkdir(parents=True, exist_ok=True)
            env["TEMP"] = str(tmp_dir)
            env["TMP"] = str(tmp_dir)
        return env

    def run(
        self,
        task: str,
        session_id: str | None = None,
        cwd: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
        permission_mode: str | None = None,
    ) -> RunResult:
        timeout = timeout or self.cfg["timeout_s"]
        cmd = self.build_command(task, session_id)
        out_path = Path(tempfile.gettempdir()) / f"dsh-feishu-out-{int(time.time() * 1000)}.ndjson"
        err_path = out_path.with_suffix(".err")
        events: list[dict] = []
        session: str | None = None
        text_parts: list[str] = []
        final_text = ""
        cancelled = False

        with open(out_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, cwd=cwd or os.getcwd(),
                                    env=self.build_env(permission_mode), stdout=out, stderr=err)
            deadline = time.time() + timeout
            pos = 0
            while True:
                alive = proc.poll() is None
                pos = _drain(out_path, pos, events, on_event)
                for ev in events:
                    if ev.get("type") == "session":
                        session = ev.get("sessionId") or session
                    elif ev.get("type") == "text":
                        text_parts.append(ev.get("text") or "")
                    elif ev.get("type") == "final":
                        final_text = ev.get("text") or final_text
                if not alive:
                    pos = _drain(out_path, pos, events, on_event)
                    break
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    break
                if time.time() > deadline:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    break
                time.sleep(0.4)

        exit_code = proc.wait()
        stderr_tail = _tail(err_path, 4000)
        for f in (out_path, err_path):
            try:
                f.unlink()
            except OSError:
                pass

        answer = final_text or "".join(text_parts)
        error = None
        if cancelled:
            error = "被 /abort 取消"
        elif exit_code != 0:
            error = stderr_tail or f"dsh 退出码 {exit_code}"
        return RunResult(
            ok=exit_code == 0 and bool(answer) and not cancelled,
            text=answer or (error or ""),
            session_id=session,
            exit_code=exit_code,
            events=events,
            stderr_tail=stderr_tail,
            error=error,
        )


def _drain(path: Path, pos: int, events: list[dict], on_event: Callable[[dict], None] | None) -> int:
    """读新增的整行 JSON,返回新的偏移。半行留给下一轮。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            chunk = f.read()
    except OSError:
        return pos
    if not chunk:
        return pos
    # 最后一行可能还没写完,留着下次读
    keep = chunk.rsplit("\n", 1)
    complete = keep[0] if len(keep) == 2 else chunk
    partial = keep[1] if len(keep) == 2 else ""
    consumed = len(complete) + (1 if len(keep) == 2 else 0)
    for line in complete.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        if on_event:
            on_event(ev)
    return pos + consumed


def _tail(path: Path, limit: int) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def describe_event(ev: dict) -> str | None:
    """给飞书看的简短活动行。"""
    t = ev.get("type")
    if t == "tool_call":
        name = ev.get("tool") or "?"
        inp = ev.get("input") or {}
        hint = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
        hint = str(hint).replace("\n", " ")[:80]
        return f"执行工具 {name}" + (f": {hint}" if hint else "")
    if t == "tool_result":
        status = ev.get("status") or "done"
        return f"工具结果: {status}"
    if t == "status":
        phase = ev.get("phase")
        if phase in ("turn_start", "step_start"):
            return f"第 {ev.get('turn')} 轮 / 第 {ev.get('step')} 步"
        if phase == "turn_end":
            return "本轮结束"
    return None


def engine_health(serve_url: str, key: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(serve_url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = [m.get("id") for m in body.get("data", [])]
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}
