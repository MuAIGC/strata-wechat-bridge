"""doctor.py — 换机器时先跑这个:逐项检查这台机器能不能跑起微信桥。

用法:python doctor.py(或 python bridge.py --doctor)
输出每项 ok/缺,并给出对应的环境变量修法。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402
from agent_runner import engine_health, find_patch, resolve_api_key  # noqa: E402


def check(name: str, ok: bool, detail: str, hint: str = "") -> str:
    line = f"[{'ok' if ok else '缺'}] {name}:{detail}"
    if not ok and hint:
        line += f" → {hint}"
    return line


def run_checks() -> list[str]:
    out: list[str] = []

    node = paths.node_bin()
    ok = node == "node" or Path(node).is_file()
    out.append(check("node", ok, node, "设 DSH_NODE 指向 node,或把 node 放进 PATH"))

    hb = paths.harness_bin()
    ok = bool(hb) and Path(hb).is_file()
    out.append(check("harness(bin.js)", ok, hb or "(没找到)",
                     "装 Strata/deepseek-harness,或设 DSH_HARNESS_BIN"))

    home = paths.dsh_home()
    out.append(check("dsh_home", home.is_dir(), str(home), "设 DSH_HOME 指向可写的 dsh home"))

    tmp = None
    try:
        tmp = paths.temp_dir()
        out.append(check("临时目录", True, str(tmp), ""))
    except RuntimeError as e:
        out.append(check("临时目录", False, str(e), "设 WECHAT_TMP_DIR 指向可写目录"))

    pd = paths.patch_dir()
    patch = find_patch(pd)
    out.append(check("profile patch", bool(patch), f"{pd} → {patch or '(没有 profile-*.patch.yml)'}",
                     "本机没配模型时先在 Strata 里配好,或设 DSH_PATCH_DIR"))

    kd = paths.key_dir()
    out.append(check("密钥目录", kd.is_dir(), str(kd), "设 DSH_KEY_DIR"))

    serve = paths.serve_url()
    try:
        key = os.environ.get(paths.key_env()) or resolve_api_key(kd, serve)[0]
    except Exception as e:
        key = ""
        out.append(check("拿密钥", False, str(e)[:120], "把 api_key 写进 wechat.config.json 的 agent.api_key"))
    health = engine_health(serve, key)
    out.append(check("本机模型服务", health["ok"],
                     str(health.get("models") if health["ok"] else health.get("error")),
                     "确认 Strata 正在 serving,或设 DSH_SERVE_URL"))

    for mod, why in (("qrcode", "二维码 PNG"), ("Crypto.Cipher", "图片/文件附件的 AES")):
        found = importlib.util.find_spec(mod) is not None
        out.append(check(f"依赖 {mod}({why})", found,
                         "装了" if found else "没装",
                         "pip install -r requirements.txt(不装也能跑纯文字对话)"))

    acc = HERE / "weixin-account.json"
    out.append(check("微信绑定", acc.is_file(), str(acc),
                     "跑 bridge.py --login --and-serve 扫码"))

    return out


def main() -> None:
    print("=== 微信桥环境检查 ===")
    for line in run_checks():
        print(line)
    print("\n全 ok 就能跑:powershell -ExecutionPolicy Bypass -File run.ps1 / bash run.sh")


if __name__ == "__main__":
    main()
