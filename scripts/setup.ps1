param(
    [switch] $PortableOcr
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonCommand = Get-Command python -ErrorAction Stop
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$agentReach = Join-Path $projectRoot '.venv\Scripts\agent-reach.exe'

& $pythonCommand.Source -m venv (Join-Path $projectRoot '.venv')
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e "$projectRoot[crawl]"
if ($PortableOcr) {
    & $venvPython -m pip install -e "$projectRoot[ocr]"
}

& $agentReach install --channels opencli

$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $envPath
}

Write-Host ''
Write-Host '安装完成。请在自己的 Chrome 登录小红书，然后运行：'
Write-Host "  $agentReach doctor --json"
Write-Host '启动看板：powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1'
