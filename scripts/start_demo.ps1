param(
    [int]$WebPort = 8080,
    [int]$RpaPort = 8090,
    [ValidateRange(1, 100)]
    [int]$PortSearchLimit = 20,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDir = Join-Path $projectRoot "tmp\demo_runtime"
$statePath = Join-Path $runtimeDir "processes.json"
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$systemPythonPath = (Get-Command python -ErrorAction Stop).Source
$venvDir = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvDir "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$requirementsMarker = Join-Path $venvDir ".requirements.sha256"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "首次运行：正在创建项目独立Python环境..."
    & $systemPythonPath -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
        throw "无法创建Python虚拟环境，请确认Python 3.11+包含venv组件。"
    }
}

$requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $requirementsMarker) {
    (Get-Content -LiteralPath $requirementsMarker -Raw -Encoding UTF8).Trim()
} else {
    ""
}
if ($installedHash -ne $requirementsHash) {
    Write-Host "首次运行或依赖已更新：正在安装项目依赖，请保持网络连接..."
    & $pythonPath -m pip install --disable-pip-version-check -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "项目依赖安装失败，请检查网络、代理或requirements.txt。"
    }
    Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -Encoding UTF8 -NoNewline
}

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

function Get-OwnedDemoProcess {
    param(
        [object]$ProcessId,
        [string]$Marker
    )
    if (-not $ProcessId) {
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$ProcessId)" -ErrorAction SilentlyContinue
    if (
        -not $process -or
        -not $process.CommandLine -or
        $process.CommandLine -notlike "*$Marker*" -or
        $process.CommandLine -notlike "*$projectRoot*"
    ) {
        return $null
    }
    return $process
}

function Test-DemoHealth {
    param(
        [int]$Port,
        [ValidateSet("web", "rpa")]
        [string]$Kind
    )
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($Kind -eq "web") {
            return $health.status -eq "ok" -and $null -ne $health.data_quality
        }
        return $health.status -eq "ok" -and $null -ne $health.tasks_count
    } catch {
        return $false
    }
}

function Test-LocalPortAvailable {
    param([int]$Port)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        try { $listener.Stop() } catch { }
    }
}

function Find-AvailablePortPair {
    param(
        [int]$PreferredWebPort,
        [int]$PreferredRpaPort,
        [int]$SearchLimit
    )
    for ($offset = 0; $offset -le $SearchLimit; $offset++) {
        $candidateWeb = $PreferredWebPort + $offset
        $candidateRpa = $PreferredRpaPort + $offset
        if ($candidateWeb -gt 65535 -or $candidateRpa -gt 65535) {
            break
        }
        if (
            $candidateWeb -ne $candidateRpa -and
            (Test-LocalPortAvailable -Port $candidateWeb) -and
            (Test-LocalPortAvailable -Port $candidateRpa)
        ) {
            return [pscustomobject]@{ web = $candidateWeb; rpa = $candidateRpa; offset = $offset }
        }
    }
    throw "在首选端口之后的$SearchLimit组端口中未找到可用的Web/RPA端口，请关闭占用程序后重试。"
}

$llmRuntime = Import-LlmConfiguration

if (Test-Path -LiteralPath $statePath) {
    try {
        $oldState = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warning "检测到无法解析的旧运行记录，将忽略并重新启动。"
        $oldState = $null
    }
    $oldWeb = if ($oldState) { Get-OwnedDemoProcess -ProcessId $oldState.web_pid -Marker "app.dashboard" } else { $null }
    $oldRpa = if ($oldState) { Get-OwnedDemoProcess -ProcessId $oldState.rpa_pid -Marker "run_mock_rpa_local.py" } else { $null }
    $oldHealthy = (
        $oldState -and
        $oldWeb -and
        $oldRpa -and
        $oldState.status -eq "running" -and
        (Test-DemoHealth -Port ([int]$oldState.web_port) -Kind "web") -and
        (Test-DemoHealth -Port ([int]$oldState.rpa_port) -Kind "rpa")
    )
    if ($oldHealthy) {
        Write-Host "Demo services are already running: http://127.0.0.1:$($oldState.web_port)"
        if ($llmRuntime.configured -and -not $oldState.llm_configured) {
            Write-Warning "Qwen is configured but the running service has not loaded it. Stop and restart the demo services."
        }
        if (-not $NoBrowser) {
            Start-Process "http://127.0.0.1:$($oldState.web_port)"
        }
        exit 0
    }
    foreach ($ownedProcess in @($oldWeb, $oldRpa)) {
        if ($ownedProcess) {
            Stop-Process -Id $ownedProcess.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if ($oldState) {
        $oldState.status = "stale"
        $oldState | Add-Member -NotePropertyName stale_at -NotePropertyValue (Get-Date).ToString("o") -Force
        $oldState | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    }
}

$requestedWebPort = $WebPort
$requestedRpaPort = $RpaPort
$portPair = Find-AvailablePortPair -PreferredWebPort $requestedWebPort -PreferredRpaPort $requestedRpaPort -SearchLimit $PortSearchLimit
$WebPort = [int]$portPair.web
$RpaPort = [int]$portPair.rpa
if ($portPair.offset -gt 0) {
    Write-Warning "首选端口${requestedWebPort}/${requestedRpaPort}已被占用，已自动切换到${WebPort}/${RpaPort}。"
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
    requested_web_port = $requestedWebPort
    requested_rpa_port = $requestedRpaPort
    port_auto_selected = ($portPair.offset -gt 0)
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
