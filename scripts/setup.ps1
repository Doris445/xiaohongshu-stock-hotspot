param(
    [switch] $PortableOcr,
    [switch] $WithScrapling,
    [switch] $SkipXiaohongshu,
    [string] $PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvRoot = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$agentReach = Join-Path $venvRoot 'Scripts\agent-reach.exe'

function Write-Step([int] $number, [string] $message) {
    Write-Host "[$number/6] $message" -ForegroundColor Cyan
}

function Assert-LastExit([string] $message) {
    if ($LASTEXITCODE -ne 0) {
        throw "$message (exit code $LASTEXITCODE). Rerun setup to resume from the local cache."
    }
}

function Find-CompatiblePython {
    $candidates = [System.Collections.Generic.List[object]]::new()
    if ($PythonPath) {
        $command = Get-Command $PythonPath -ErrorAction Stop
        $candidates.Add([pscustomobject]@{ Executable = $command.Source; Args = @() })
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.13', '-3.12', '-3.11', '-3.10')) {
            $candidates.Add([pscustomobject]@{ Executable = $launcher.Source; Args = @($version) })
        }
    }
    foreach ($name in @('python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $candidates.Add([pscustomobject]@{ Executable = $command.Source; Args = @() })
        }
    }

    foreach ($candidate in $candidates) {
        $prefix = @($candidate.Args)
        & $candidate.Executable @prefix -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) and sys.maxsize > 2**32 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    throw 'No compatible 64-bit Python 3.10-3.13 found. Install Python from python.org and enable Python Launcher.'
}

Write-Step 1 'Checking Python (Microsoft Store aliases are rejected)'
$python = Find-CompatiblePython
$pythonPrefix = @($python.Args)
$pythonVersion = & $python.Executable @pythonPrefix -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "Using Python $pythonVersion : $($python.Executable) $($pythonPrefix -join ' ')"

Write-Step 2 'Creating or reusing the project virtual environment'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python.Executable @pythonPrefix -m venv $venvRoot
    Assert-LastExit 'Failed to create .venv'
}
& $venvPython -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'The existing .venv uses an incompatible Python. Remove this project .venv and rerun setup.'
}

$pipCommon = @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
    '--prefer-binary', '--retries', '3', '--timeout', '60'
)

Write-Step 3 'Installing pinned dashboard core dependencies'
& $venvPython @pipCommon -e $projectRoot
Assert-LastExit 'Failed to install dashboard core'

Write-Step 4 'Installing data connectors'
if (-not $SkipXiaohongshu) {
    & $venvPython @pipCommon -e "$projectRoot[xiaohongshu]"
    Assert-LastExit 'Failed to install the official Agent Reach package'
    & $agentReach install --env=auto --channels=xiaohongshu
    Assert-LastExit 'Failed to configure Agent Reach/OpenCLI'
} else {
    Write-Host 'Xiaohongshu connector skipped; Eastmoney and demo modes remain available.'
}
if ($WithScrapling) {
    Write-Host 'Installing optional Scrapling HTTP transport (larger dependency set)...'
    & $venvPython @pipCommon -e "$projectRoot[scrapling]"
    Assert-LastExit 'Failed to install optional Scrapling transport'
}
if ($PortableOcr) {
    Write-Host 'Installing optional portable OCR...'
    & $venvPython @pipCommon -e "$projectRoot[ocr]"
    Assert-LastExit 'Failed to install optional portable OCR'
}

Write-Step 5 'Creating local configuration without overwriting an existing .env'
$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $envPath
}

Write-Step 6 'Running deployment diagnostics'
$oldPath = $env:PATH
try {
    $env:PATH = "$(Join-Path $venvRoot 'Scripts');$oldPath"
    & $venvPython (Join-Path $projectRoot 'scripts\doctor.py')
    Assert-LastExit 'Deployment diagnostics failed'
} finally {
    $env:PATH = $oldPath
}

Write-Host ''
Write-Host 'Setup complete. Log in to Xiaohongshu in your own Chrome profile.' -ForegroundColor Green
Write-Host 'Start: powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1'
Write-Host 'If setup was interrupted, rerun this script to reuse pip cache; do not remove .venv.'
