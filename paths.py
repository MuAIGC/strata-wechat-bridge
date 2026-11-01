"""paths.py — 跨平台的默认路径(Windows / Linux)。

优先级:环境变量 > Strata 常见布局 > 报错提示。包里没有写死某台机器的路径。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _writable(p: Path) -> bool:
    """真的试一下能不能写(沙箱里"能创建目录"不等于"能写文件")。"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".probe-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def temp_dir() -> Path:
    """能写的临时目录:WECHAT_TMP_DIR 优先,然后系统 temp、/tmp、包内 .tmp、当前目录。
    受限环境(沙箱、只读 /tmp)里这点很关键:测试和 harness 都要 mkdtemp。"""
    cands: list[Path] = []
    v = _env("WECHAT_TMP_DIR")
    if v:
        cands.append(Path(v))
    for d in (os.environ.get("WECHAT_TMP_DIR"), os.environ.get("TEMP"), os.environ.get("TMP"),
              os.environ.get("TMPDIR"), "/tmp"):
        if d:
            cands.append(Path(d))
    cands.append(Path.home() / ".strata" / "tmp")
    cands += [HERE / ".tmp", Path.cwd() / ".tmp"]
    for c in cands:
        if _writable(c):
            tempfile.tempdir = str(c)
            return c
    raise RuntimeError("找不到可写的临时目录:设 WECHAT_TMP_DIR 指向一个可写目录")


def _env(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


def dsh_home() -> Path:
    """dsh 的 home(profile 缓存、会话、凭证)。DSH_HOME 优先。"""
    return Path(_env("DSH_HOME") or (Path.home() / ".strata" / "data" / "agent" / "home"))


def _first_dir(cands: list[Path]) -> Path:
    for c in cands:
        if c.is_dir():
            return c
    return cands[-1] if cands else Path(".")


def patch_dir() -> Path:
    """放 host profile patch 的目录。注意它跟 DSH_HOME 不一定同处:被覆盖的 DSH_HOME 在别处时,
    还要试 Strata 的默认布局 ~/.strata/data/agent。"""
    env = _env("DSH_PATCH_DIR")
    if env:
        return Path(env)
    home = dsh_home()
    return _first_dir([home.parent, Path.home() / ".strata" / "data" / "agent"])


def key_dir() -> Path:
    """本机模型 API key 的目录(试 ~/.strata/data/native-instances 和 DSH_HOME 推出来的位置)。"""
    env = _env("DSH_KEY_DIR")
    if env:
        return Path(env)
    home = dsh_home()
    cands = [Path.home() / ".strata" / "data" / "native-instances"]
    try:
        cands.append(home.parents[1] / "native-instances")
    except IndexError:
        pass
    cands.append(Path.home() / ".strata" / "native-instances")
    return _first_dir(cands)


def node_bin() -> str:
    """node 可执行文件。DSH_NODE 优先;Windows 上试 C:\\Strata\\node\\node.exe;否则用 PATH 里的 node。"""
    env = _env("DSH_NODE")
    if env:
        return env
    if os.name == "nt":
        for cand in (Path("C:/Strata/node/node.exe"), Path("C:/Program Files/nodejs/node.exe")):
            if cand.exists():
                return str(cand)
    return "node"


def harness_bin() -> str:
    """deepseek-harness 的 lib/bin.js。DSH_HARNESS_BIN 优先;再试几个常见位置。"""
    env = _env("DSH_HARNESS_BIN")
    if env:
        return env
    cands = []
    if os.name == "nt":
        cands += [Path("C:/Strata/harness/lib/bin.js")]
    cands += [
        Path.home() / ".strata" / "harness" / "lib" / "bin.js",
        Path.home() / ".strata" / "data" / "harness" / "lib" / "bin.js",
        Path.home() / ".deepseek-harness" / "lib" / "bin.js",
        # npm 全局装的 deepseek-harness(Linux/macOS 常见)
        Path("/usr/local/lib/node_modules/deepseek-harness/lib/bin.js"),
        Path("/usr/lib/node_modules/deepseek-harness/lib/bin.js"),
        Path.home() / ".npm-global" / "lib" / "node_modules" / "deepseek-harness" / "lib" / "bin.js",
    ]
    for c in cands:
        if c.is_file():
            return str(c)
    return ""


def serve_url() -> str:
    """本机模型的 OpenAI 兼容接口(用来挑可用的 API key)。"""
    return _env("DSH_SERVE_URL") or _env("STRATA_SERVE_URL") or "http://127.0.0.1:18500/v1/models"


def key_env() -> str:
    return _env("DSH_KEY_ENV") or "STRATA_LOCAL_KEY_0"


def workspace() -> Path:
    """桥的工作目录:STRATA_WORKSPACE / WECHAT_WORKSPACE 优先,否则当前目录。"""
    return Path(_env("WECHAT_WORKSPACE") or _env("STRATA_WORKSPACE") or Path.cwd())


def tmp_dir() -> Path | None:
    """给 harness 的临时目录(spill-local 要 mkdtemp;沙箱里 %TEMP% 可能不能写)。"""
    v = _env("WECHAT_TMP_DIR")
    return Path(v) if v else None


def describe() -> dict[str, str]:
    """打印给 --status 用的一份路径快照。"""
    hb = harness_bin()
    return {
        "dsh_home": str(dsh_home()),
        "patch_dir": str(patch_dir()),
        "key_dir": str(key_dir()),
        "node": node_bin(),
        "harness_bin": hb or "(没找到,设 DSH_HARNESS_BIN)",
        "serve_url": serve_url(),
        "workspace": str(workspace()),
        "os": os.name,
    }
