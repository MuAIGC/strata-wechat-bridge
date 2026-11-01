# setup.ps1 — Windows 装包:建 venv、装依赖、编译、跑离线测试、打状态。
param([switch]$SkipTests, [switch]$NoStatus, [switch]$NoDeps)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv'

if (-not (Test-Path $venv)) {
  Write-Host "creating venv: $venv"
  python -m venv $venv
  if ($LASTEXITCODE -ne 0) { exit 1 }
}
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $venv 'bin/python' }   # 万一在类 Unix 上跑

if ($NoDeps) {
  Write-Host "跳过装依赖(零依赖也能跑登录链接和纯文字对话)"
} else {
  Write-Host "installing deps"
  & $py -m pip install -r (Join-Path $root 'requirements.txt') 2>&1 | Out-Host
  if ($LASTEXITCODE -ne 0) {
    Write-Host "pip 失败(受限环境常见)。继续不带依赖跑:setup.ps1 -NoDeps -SkipTests"
    exit 1
  }
}

Write-Host "compiling"
& $py -m py_compile (Get-ChildItem $root -Filter '*.py' | ForEach-Object { $_.FullName }) 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "compile failed"; exit 1 }

if (-not $SkipTests) {
  Write-Host "`n=== offline tests ==="
  & $py (Join-Path $root 'run_tests.py') 2>&1 | Out-Host
  if ($LASTEXITCODE -ne 0) { Write-Host "tests failed"; exit 1 }
}

if (-not $NoStatus) {
  Write-Host "`n=== doctor(这台机器能不能跑起来) ==="
  & $py (Join-Path $root 'doctor.py') 2>&1 | Out-Host
  Write-Host "`n=== status ==="
  & $py (Join-Path $root 'bridge.py') --status 2>&1 | Out-Host
}
exit 0
