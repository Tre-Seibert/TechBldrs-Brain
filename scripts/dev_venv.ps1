# Machine-local venv for this repo when the working copy lives in OneDrive.
# Puts the venv under %LOCALAPPDATA% so it is NOT synced and cannot overwrite
# another machine's interpreter.
#
# Usage (from repo root):
#   . .\scripts\dev_venv.ps1          # create if needed, then activate
#   . .\scripts\dev_venv.ps1 -Install # also pip install -r requirements.txt

param(
    [switch]$Install
)

$ErrorActionPreference = "Stop"

$venvDir = Join-Path $env:LOCALAPPDATA "venvs\tb-brain"
$python = Join-Path $venvDir "Scripts\python.exe"
$activate = Join-Path $venvDir "Scripts\Activate.ps1"
$repoRoot = Split-Path -Parent $PSScriptRoot
$requirements = Join-Path $repoRoot "requirements.txt"

if (-not (Test-Path $python)) {
    Write-Host "Creating machine-local venv at $venvDir"
    New-Item -ItemType Directory -Force -Path (Split-Path $venvDir) | Out-Null
    py -3.11 -m venv $venvDir
}

$fastapiHint = Join-Path $venvDir "Lib\site-packages\fastapi"
if ($Install -or -not (Test-Path $fastapiHint)) {
    Write-Host "Installing dependencies from requirements.txt"
    & $python -m pip install --upgrade pip
    & $python -m pip install -r $requirements
}

. $activate
Write-Host "Active venv: $venvDir"
Write-Host "Python: $((Get-Command python).Source)"
