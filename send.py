"""send.py — 从桌面这一侧往微信发一条(共用桥的凭证和 context_token,回到同一条会话)。

用法:
  python send.py "要发的文本"                # 发给 allow_from 里的第一个对端
  python send.py --peer <ilink_user_id> "文本"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from weixin_api import WeixinApi  # noqa: E402

CONFIG_PATH = HERE / "wechat.config.json"
ACCOUNT_PATH = HERE / "weixin-account.json"
SESSIONS_PATH = HERE / "sessions.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def send(text: str, peer: str | None = None) -> dict:
    acc = load_json(ACCOUNT_PATH, {})
    if not acc.get("token"):
        raise RuntimeError("还没扫码登录(weixin-account.json 里没有 token)")
    cfg = load_json(CONFIG_PATH, {})
    sess = load_json(SESSIONS_PATH, {})
    peer = peer or (cfg.get("allow_from") or [None])[0] or next(iter(sess), None)
    if not peer:
        raise RuntimeError("不知道发给谁:allow_from 和 sessions.json 都是空的")

    api = WeixinApi(acc.get("base_url") or "https://ilinkai.weixin.qq.com",
                    acc["token"], cfg.get("bot_agent") or "Strata/dsh")
    ctx = sess.get(peer, {}).get("context_token")
    resp = api.send_text(peer, text, ctx)
    if resp.get("ret") not in (0, None) or resp.get("errcode") not in (0, None):
        raise RuntimeError(f"发送失败:{resp}")

    # 记进对话记录,桌面这边读到的和手机上看到的一致
    import time
    workspace = cfg.get("workspace") or HERE.parent
    try:
        with open(Path(workspace) / "wechat-chat.md", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] 桌面→手机({peer[-12:]}): {text}\n")
    except OSError:
        pass
    return resp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", default=None)
    ap.add_argument("text")
    args = ap.parse_args()
    resp = send(args.text, args.peer)
    print(f"已发送:{resp.get('message_id') or resp}")


if __name__ == "__main__":
    main()
