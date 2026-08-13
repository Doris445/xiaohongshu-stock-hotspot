$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Run scripts/setup.ps1 first.'
}
$venvScripts = Join-Path $projectRoot '.venv\Scripts'
$oldPath = $env:PATH
try {
    # Directly invoking venv Python does not activate the venv. Prepending its
    # Scripts directory lets the backend reliably find agent-reach.exe.
    $env:PATH = "$venvScripts;$oldPath"
    & $python (Join-Path $projectRoot 'scripts\doctor.py') --quick
    if ($LASTEXITCODE -ne 0) { throw 'Deployment diagnostics failed. Rerun scripts/setup.ps1.' }
    & $python (Join-Path $projectRoot 'server.py') @args
} finally {
    $env:PATH = $oldPath
}
