param(
    [int]$Repeats = 1,
    [string]$OutputJson = "",
    [string]$OutputCsv = ""
)

$ErrorActionPreference = "Stop"
if ($Repeats -lt 1 -or $Repeats -gt 5) {
    throw "Repeats must be between 1 and 5."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$configDir = Join-Path $localAppData "ChuangLingJingCostIntelligence"
$configPath = Join-Path $configDir "llm-config.json"
$secretPath = Join-Path $configDir "qwen-api-key.dpapi"
if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $secretPath)) {
    throw "Qwen is not configured. Run scripts\configure_qwen.ps1 first."
}

$pointer = [IntPtr]::Zero
$plainKey = $null
$previousPythonUtf8 = $env:PYTHONUTF8
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
    $env:PYTHONUTF8 = "1"
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $pythonArguments = @(
        (Join-Path $PSScriptRoot "llm_golden_evaluation.py"),
        "--project-root", $projectRoot,
        "--repeats", [string]$Repeats,
        "--expected-model", [string]$config.model,
        "--live-llm"
    )
    if (-not [string]::IsNullOrWhiteSpace($OutputJson)) {
        $pythonArguments += @("--output-json", $OutputJson)
    }
    if (-not [string]::IsNullOrWhiteSpace($OutputCsv)) {
        $pythonArguments += @("--output-csv", $OutputCsv)
    }
    & $pythonPath @pythonArguments
    if ($LASTEXITCODE -ne 0) {
        throw "The LLM golden-scenario acceptance gate failed with exit code $LASTEXITCODE."
    }
} finally {
    Remove-Item Env:COST_LLM_API_KEY -ErrorAction SilentlyContinue
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
    $plainKey = $null
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}
