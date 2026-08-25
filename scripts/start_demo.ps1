param(
    [int]$WebPort = 8080,
    [int]$RpaPort = 8090,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDir = Join-Path $projectRoot "tmp\demo_runtime"
$statePath = Join-Path $runtimeDir "processes.json"
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$pythonPath = (Get-Command python -ErrorAction Stop).Source

function Import-LlmConfiguration {
    $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $configDir = Join-Path $localAppData "ChuangLingJingCostIntelligence"
    $configPath = Join-Path $configDir "llm-config.json"
    $secretPath = Join-Path $configDir "qwen-api-key.dpapi"
    if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $secretPath)) {
        return [pscustomobject]@{ configured = $false; provider = $null; model = $null }
    }

    $pointer = [IntPtr]::Zero
    $plainKey = $null
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.provider -ne "dashscope" -or $config.api_style -ne "chat_completions") {
            throw "Unsupported local LLM provider configuration."
        }
        $secureKey = Get-Content -LiteralPath $secretPath -Raw -Encoding UTF8 | ConvertTo-SecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plainKey)) {
            throw "The local LLM credential is empty."
        }
        $env:COST_LLM_ENABLED = "true"
        $env:COST_LLM_PROVIDER = [string]$config.provider
        $env:COST_LLM_API_STYLE = [string]$config.api_style
        $env:COST_LLM_BASE_URL = [string]$config.base_url
        $env:COST_LLM_MODEL = [string]$config.model
        $env:COST_LLM_API_KEY = $plainKey
        return [pscustomobject]@{
            configured = $true
            provider = [string]$config.provider
            model = [string]$config.model
        }
    } catch {
        Write-Warning "Local LLM configuration could not be loaded; deterministic fallback remains active. $($_.Exception.Message)"
        return [pscustomobject]@{ configured = $false; provider = $null; model = $null }
    } finally {
        $plainKey = $null
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

$llmRuntime = Import-LlmConfiguration

if (Test-Path -LiteralPath $statePath) {
    $oldState = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $oldWeb = Get-Process -Id $oldState.web_pid -ErrorAction SilentlyContinue
    $oldRpa = Get-Process -Id $oldState.rpa_pid -ErrorAction SilentlyContinue
    if ($oldWeb -and $oldRpa -and $oldState.status -eq "running") {
        Write-Host "Demo services are already running: http://127.0.0.1:$($oldState.web_port)"
        if ($llmRuntime.configured -and -not $oldState.llm_configured) {
            Write-Warning "Qwen is configured but the running service has not loaded it. Stop and restart the demo services."
        }
        if (-not $NoBrowser) {
            Start-Process "http://127.0.0.1:$($oldState.web_port)"
        }
        exit 0
    }
}

$preflightArgs = @(
    (Join-Path $PSScriptRoot "demo_preflight.py"),
    "--project-root", $projectRoot,
    "--web-port", [string]$WebPort,
    "--rpa-port", [string]$RpaPort,
    "--json-output", "tmp\demo_runtime\preflight.json"
)
& $pythonPath @preflightArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Preflight failed. Demo services were not started."
}

$rpaOut = Join-Path $runtimeDir "mock-rpa.out.log"
$rpaErr = Join-Path $runtimeDir "mock-rpa.err.log"
$webOut = Join-Path $runtimeDir "dashboard.out.log"
$webErr = Join-Path $runtimeDir "dashboard.err.log"
$rpaArgs = @(
    (Join-Path $PSScriptRoot "run_mock_rpa_local.py"),
    "--project-root", $projectRoot,
    "--port", [string]$RpaPort
)
$webArgs = @(
    "-m", "app.dashboard",
    "--data-dir", $projectRoot,
    "--host", "127.0.0.1",
    "--port", [string]$WebPort,
    "--rpa-base-url", "http://127.0.0.1:$RpaPort"
)

$rpaProcess = Start-Process -FilePath $pythonPath -ArgumentList $rpaArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $rpaOut -RedirectStandardError $rpaErr -PassThru
$webProcess = Start-Process -FilePath $pythonPath -ArgumentList $webArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $webOut -RedirectStandardError $webErr -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $rpaHealth = Invoke-RestMethod "http://127.0.0.1:$RpaPort/health" -TimeoutSec 1
        $webHealth = Invoke-RestMethod "http://127.0.0.1:$WebPort/health" -TimeoutSec 2
        if ($rpaHealth.status -eq "ok" -and $webHealth.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
    }
    Start-Sleep -Milliseconds 250
}

if (-not $ready) {
    Stop-Process -Id $rpaProcess.Id,$webProcess.Id -Force -ErrorAction SilentlyContinue
    Write-Host "Mock RPA error log:" -ForegroundColor Red
    Get-Content -LiteralPath $rpaErr -ErrorAction SilentlyContinue
    Write-Host "Web error log:" -ForegroundColor Red
    Get-Content -LiteralPath $webErr -ErrorAction SilentlyContinue
    Write-Error "Health checks failed. Processes started by this run were stopped."
}

$state = [ordered]@{
    status = "running"
    started_at = (Get-Date).ToString("o")
    project_root = $projectRoot
    python = $pythonPath
    web_pid = $webProcess.Id
    rpa_pid = $rpaProcess.Id
    web_port = $WebPort
    rpa_port = $RpaPort
    web_url = "http://127.0.0.1:$WebPort"
    rpa_url = "http://127.0.0.1:$RpaPort"
    llm_configured = $llmRuntime.configured
    llm_provider = $llmRuntime.provider
    llm_model = $llmRuntime.model
}
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

Write-Host "Demo services started successfully." -ForegroundColor Green
Write-Host "Dashboard: http://127.0.0.1:$WebPort"
Write-Host "Mock RPA: http://127.0.0.1:$RpaPort"
if ($llmRuntime.configured) {
    Write-Host "LLM: $($llmRuntime.provider) / $($llmRuntime.model)" -ForegroundColor Green
} else {
    Write-Host "LLM: deterministic fallback (run the Qwen configuration launcher to enable)" -ForegroundColor Yellow
}
Write-Host "Use the stop launcher in the project root to stop both services."

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:$WebPort"
}
