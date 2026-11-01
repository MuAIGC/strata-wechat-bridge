#!/usr/bin/env bash
# run.sh — Linux/macOS:没绑定就出扫码链接并立刻常驻;已绑定直接常驻。
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
py="$root/.venv/bin/python"
bridge="$root/bridge.py"

if [ ! -x "$py" ]; then
  echo "先跑 setup.sh"
  exit 1
fi

status="$("$py" "$bridge" --status 2>&1 || true)"
echo "$status"
if echo "$status" | grep -q 'token:none'; then
  echo
  echo "未绑定:出二维码链接(手机浏览器打开再用微信扫),确认后立刻开始收消息"
  "$py" -u "$bridge" --login --and-serve
else
  echo
  echo "常驻收消息中(Ctrl+C 退出)"
  "$py" -u "$bridge"
fi
