# run.ps1 — Windows:没绑定就出扫码链接并立刻常驻;已绑定直接常驻。
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $root '.venv\bin\python' }
$bridge = Join-Path $root 'bridge.py'
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path $py)) { Write-Host "先跑 setup.ps1"; exit 1 }

$status = & $py $bridge --status 2>&1 | Out-String
Write-Host $status
if ($status -match 'token:none') {
  Write-Host "`n未绑定:出二维码链接(手机浏览器打开再用微信扫),确认后立刻开始收消息"
  & $py -u $bridge --login --and-serve
} else {
  Write-Host "`n常驻收消息中(Ctrl+C 退出)"
  & $py -u $bridge
}
