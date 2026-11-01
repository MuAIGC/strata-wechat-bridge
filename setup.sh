#!/usr/bin/env bash
# setup.sh — Linux/macOS 装包:venv、依赖、编译、离线测试、状态。
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
venv="$root/.venv"
skip_tests=0
no_deps=0
no_status=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) skip_tests=1 ;;
    --no-deps) no_deps=1 ;;
    --no-status) no_status=1 ;;
  esac
done

if [ ! -d "$venv" ]; then
  echo "creating venv: $venv"
  python3 -m venv "$venv"
fi
py="$venv/bin/python"

if [ "$no_deps" -eq 1 ]; then
  echo "跳过装依赖(零依赖也能跑登录链接和纯文字对话)"
else
  echo "installing deps"
  "$py" -m pip install -r "$root/requirements.txt" || {
    echo "pip 失败(受限环境常见)。继续不带依赖跑: bash setup.sh --no-deps --skip-tests"
    exit 1
  }
fi

echo "compiling"
"$py" -m py_compile "$root"/*.py

if [ "$skip_tests" -eq 0 ]; then
  echo
  echo "=== offline tests ==="
  "$py" "$root/run_tests.py"
fi

if [ "$no_status" -eq 0 ]; then
  echo
  echo "=== doctor(这台机器能不能跑起来) ==="
  "$py" "$root/doctor.py"
  echo
  echo "=== status ==="
  "$py" "$root/bridge.py" --status
fi
