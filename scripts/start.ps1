$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '未找到 .venv，请先运行 scripts/setup.ps1。'
}
& $python (Join-Path $projectRoot 'server.py') @args
