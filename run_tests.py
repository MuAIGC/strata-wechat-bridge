"""run_tests.py — 在包根目录跑全部离线测试(Windows/Linux 都行,不需要凭据)。

用法:python run_tests.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402

paths.temp_dir()   # 系统 temp 不可用时退回包内 .tmp
TESTS = sorted((HERE / "tests").glob("test_*.py"))

failed = 0
for t in TESTS:
    print(f"== {t.name} ==", flush=True)
    # 子进程输出写文件而不是管道:受限沙箱里程序不能开命名管道,普通终端也照样能用
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "out.log"
        with open(log, "w", encoding="utf-8") as f:
            proc = subprocess.run([sys.executable, str(t)], cwd=str(HERE), stdout=f, stderr=f)
        try:
            print(log.read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            pass
    if proc.returncode != 0:
        failed += 1
        print(f"[失败] {t.name}(退出码 {proc.returncode})")
print(f"\n共 {len(TESTS)} 个测试,{failed} 个失败。")
sys.exit(1 if failed else 0)
