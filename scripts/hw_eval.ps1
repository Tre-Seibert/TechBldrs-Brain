# A4 validation in one go: the Flow eval in legacy and lean prompt styles at 8K and 16K Ollama context,
# the prompt-cache probe, and the comparisons. Run on the runtime box with tb-brain stopped.
#
# Usage (from repo root):  .\scripts\hw_eval.ps1
#
# Full logs and a short summary.txt (the only thing to paste back) go to
# %LOCALAPPDATA%\tb-brain\hw-eval\<timestamp>\. .env and OLLAMA_CONTEXT_LENGTH are put back afterwards,
# even if a step fails. tb-brain is started and stopped by the script; start it yourself again after.

param(
    [string]$OllamaApp = (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe")
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
. (Join-Path $PSScriptRoot "dev_venv.ps1")
$py = (Get-Command python).Source
$env:PYTHONIOENCODING = "utf-8"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $env:LOCALAPPDATA "tb-brain\hw-eval\$stamp"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$summary = Join-Path $out "summary.txt"
$envFile = Join-Path $repo ".env"
$envBackup = Join-Path $out "env.backup"
$ollamaLogDir = Join-Path $env:LOCALAPPDATA "Ollama"
$brainUrl = "http://127.0.0.1:8765"
$ollamaUrl = "http://127.0.0.1:11434"
$brain = $null

function Note([string]$text) {
    Add-Content -Path $summary -Value $text -Encoding UTF8
    Write-Host $text
}

function Get-Url([string]$url) {
    try { return Invoke-RestMethod -Uri $url -TimeoutSec 3 } catch { return $null }
}

function Wait-Url([string]$url, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $reply = Get-Url $url
        if ($null -ne $reply) { return $reply }
        Start-Sleep -Milliseconds 500
    }
    throw "No answer from $url after $seconds s"
}

function Set-EnvLine([string]$name, [string]$value) {
    $lines = [System.Collections.Generic.List[string]]([IO.File]::ReadAllLines($envFile))
    $index = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "^\s*$name\s*=") { $index = $i } }
    if ($index -ge 0) { $lines[$index] = "$name=$value" } else { $lines.Add("$name=$value") }
    [IO.File]::WriteAllLines($envFile, $lines)
}

function Get-EnvLine([string]$name) {
    foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match "^\s*$name\s*=\s*(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}

function Restart-Ollama([string]$context) {
    # The app inherits this process's environment, so set it here as well as for the user.
    [Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", $context, "User")
    $env:OLLAMA_CONTEXT_LENGTH = $context
    Get-Process "ollama*", "llama-server" -ErrorAction SilentlyContinue | Stop-Process -Force  # runners outlive the app
    Start-Sleep -Seconds 2
    Start-Process -FilePath $OllamaApp | Out-Null
    Wait-Url "$ollamaUrl/api/version" 60 | Out-Null
    # Load the model once so the first eval question doesn't pay for it.
    $model = Get-EnvLine "LLM_MODEL"
    $body = @{ model = $model; prompt = "hi"; stream = $false; options = @{ num_predict = 1 } } | ConvertTo-Json
    Invoke-RestMethod -Uri "$ollamaUrl/api/generate" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 300 | Out-Null
}

function Start-Brain([string]$label) {
    $script:brain = Start-Process -FilePath $py -ArgumentList "-m", "app" -WorkingDirectory $repo -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $out "brain-$label.out.log") -RedirectStandardError (Join-Path $out "brain-$label.err.log")
    $health = Wait-Url "$brainUrl/health" 60
    if ($health.flow_mode -ne "stub") { throw "tb-brain is not on stub data (flow_mode=$($health.flow_mode)); refusing to run the eval" }
    return $health
}

function Stop-Brain {
    if ($null -ne $script:brain -and -not $script:brain.HasExited) { Stop-Process -Id $script:brain.Id -Force }
    $script:brain = $null
    Start-Sleep -Seconds 2
}

function Invoke-Py([string]$name, [string[]]$arguments) {
    $log = Join-Path $out "$name.log"
    Start-Process -FilePath $py -ArgumentList $arguments -WorkingDirectory $repo -NoNewWindow -Wait `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $out "$name.err.log") | Out-Null
    return $log
}

function Get-Truncations([string]$filter = "server.log") {
    $files = Get-ChildItem -Path $ollamaLogDir -Filter $filter -ErrorAction SilentlyContinue
    if (-not $files) { return 0 }
    return @(Select-String -Path $files.FullName -Pattern "truncating input prompt").Count
}

function Invoke-Eval([string]$style, [string]$context, [string]$label) {
    Note ""
    Note "=== $label (PROMPT_STYLE=$style, context=$context) ==="
    $health = Start-Brain $label
    Note "health: prompt_style=$($health.prompt_style) flow=$($health.flow_mode) model=$($health.llm_model)"
    $log = Invoke-Py "eval-$label" @("scripts\eval.py", "run", "--questions", "eval\flow_cases.json", "--label", $label)
    $lines = Get-Content $log
    $start = ($lines | Select-String -Pattern "passed, avg" | Select-Object -Last 1).LineNumber
    if ($start) {
        foreach ($line in $lines[($start - 1)..($lines.Count - 1)]) {
            if ($line -match "^saved ") { break }
            Note $line
        }
    } else {
        Note "no score line; see $log"
    }
    $failed = @($lines | Where-Object { $_ -match "^FAIL" })
    Note "failed cases: $($failed.Count)"
    foreach ($line in $failed) { Note "  $line" }
    $ps = (cmd /c "ollama ps 2>&1") -join "`n"
    Note "ollama ps:`n$ps"
    Note "truncation warnings in Ollama log since restart: $(Get-Truncations)"
    Stop-Brain
}

try {
    if ($null -ne (Get-Url "$brainUrl/health")) { throw "tb-brain is already running on $brainUrl. Stop it (Ctrl+C in its window), then run this again." }
    if (-not (Test-Path $OllamaApp)) { throw "Ollama app not found at $OllamaApp. Pass -OllamaApp with its path (Get-Command ollama shows where it is)." }

    Copy-Item $envFile $envBackup
    $userContext = [Environment]::GetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", "User")
    $machineContext = [Environment]::GetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", "Machine")

    Note "tb-brain hardware eval $stamp"
    Note "commit: $((cmd /c "git log --oneline -1 2>&1") -join '')"
    Note "model: $(Get-EnvLine 'LLM_MODEL')"
    Note "OLLAMA_CONTEXT_LENGTH before: user='$userContext' machine='$machineContext'"
    Note "truncation warnings already in Ollama logs (real use): $(Get-Truncations 'server*.log')"
    Note "qdrant on 6333: $(if ($null -ne (Get-Url 'http://127.0.0.1:6333/healthz')) { 'up' } else { 'down' })"

    Set-EnvLine "FLOW_MODE" "stub"

    Set-EnvLine "PROMPT_STYLE" "legacy"
    Restart-Ollama "8192"
    Invoke-Eval "legacy" "8192" "legacy-8k"

    Restart-Ollama "16384"
    Invoke-Eval "legacy" "16384" "legacy-16k"

    Set-EnvLine "PROMPT_STYLE" "lean"
    Restart-Ollama "16384"
    Invoke-Eval "lean" "16384" "lean-16k"

    Note ""
    Note "=== cache probe (16K, nothing else using the model) ==="
    foreach ($style in "legacy", "lean") {
        $log = Invoke-Py "probe-$style" @("scripts\probe_cache.py", "--style", $style)
        foreach ($line in Get-Content $log) { Note $line }
        $err = Get-Content (Join-Path $out "probe-$style.err.log") -ErrorAction SilentlyContinue
        if ($err) { Note "probe-$style stderr: $(($err | Select-Object -Last 3) -join ' | ')" }
    }

    Restart-Ollama "8192"
    Invoke-Eval "lean" "8192" "lean-8k"

    Note ""
    Note "=== comparisons (B fixed / B broke) ==="
    $results = Join-Path $repo "eval\results"
    function Last([string]$label) { (Get-ChildItem -Path $results -Filter "*-$label.json" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName }
    foreach ($pair in @(@("legacy-8k", "legacy-16k"), @("legacy-16k", "lean-16k"), @("legacy-8k", "lean-8k"))) {
        Note "-- $($pair[0]) vs $($pair[1])"
        $log = Invoke-Py "compare-$($pair[0])-vs-$($pair[1])" @("scripts\eval.py", "compare", (Last $pair[0]), (Last $pair[1]))
        foreach ($line in Get-Content $log) {
            if ($line -match "^(A|B):|passed, avg|^B (fixed|broke)") { Note $line }
        }
    }
}
catch {
    Note ""
    Note "STOPPED: $($_.Exception.Message)"
}
finally {
    Stop-Brain
    if (Test-Path $envBackup) { Copy-Item $envBackup $envFile -Force }
    if ($null -ne (Get-Variable -Name userContext -ErrorAction SilentlyContinue)) {
        $restore = if ([string]::IsNullOrEmpty($userContext)) { $null } else { $userContext }
        [Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", $restore, "User")
        $env:OLLAMA_CONTEXT_LENGTH = $restore
        Get-Process "ollama*", "llama-server" -ErrorAction SilentlyContinue | Stop-Process -Force  # runners outlive the app
        Start-Sleep -Seconds 2
        Start-Process -FilePath $OllamaApp | Out-Null
    }
    Note ""
    Note "Restored .env and OLLAMA_CONTEXT_LENGTH. Start tb-brain again with .\scripts\run.ps1"
    Note "Full logs: $out"
    Write-Host ""
    Write-Host "Paste back the contents of: $summary"
}
