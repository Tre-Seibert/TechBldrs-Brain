# Run tb-brain from the machine-local venv (not a repo .venv).
# Usage (from repo root):  .\scripts\run.ps1

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
. (Join-Path $PSScriptRoot "dev_venv.ps1")
python -m app
