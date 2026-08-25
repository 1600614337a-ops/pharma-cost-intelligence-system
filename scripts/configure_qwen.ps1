param(
    [string]$BaseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    [string]$Model = "qwen3.8-max",
    [switch]$SkipConnectionTest,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.Trim().TrimEnd("/")
$Model = $Model.Trim().ToLowerInvariant()

$parsedUrl = $null
if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or $parsedUrl.Scheme -ne "https") {
    throw "Base URL must be an absolute HTTPS address."
}
if ($Model -ne "qwen3.8-max") {
    throw "This launcher is locked to qwen3.8-max."
}

if ($ValidateOnly) {
    Write-Host "Qwen configuration launcher is ready."
    Write-Host "Base URL: $BaseUrl"
    Write-Host "Model: $Model"
    exit 0
}

$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$configDir = Join-Path $localAppData "ChuangLingJingCostIntelligence"
$configPath = Join-Path $configDir "llm-config.json"
$secretPath = Join-Path $configDir "qwen-api-key.dpapi"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null

Write-Host "Configure Alibaba Cloud Model Studio (Qwen)" -ForegroundColor Cyan
Write-Host "Region: China North 2 (Beijing)"
Write-Host "Base URL: $BaseUrl"
Write-Host "Model: $Model"
Write-Host "The API Key will be encrypted with Windows DPAPI and will not be written to the project directory."

$secureKey = Read-Host "Enter the Alibaba Cloud Model Studio API Key" -AsSecureString
if ($secureKey.Length -lt 8) {
    throw "The API Key is empty or too short."
}

$secureKey | ConvertFrom-SecureString | Set-Content -LiteralPath $secretPath -Encoding UTF8 -NoNewline
$config = [ordered]@{
    enabled = $true
    provider = "dashscope"
    api_style = "chat_completions"
    region = "cn-beijing"
    base_url = $BaseUrl
    model = $Model
    secret_path = $secretPath
}
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8

Write-Host "Encrypted credential saved for the current Windows user." -ForegroundColor Green
Write-Host "Configuration: $configPath"

if (-not $SkipConnectionTest) {
    $answer = Read-Host "Run a minimal connection test now? This incurs a small API charge. [Y/N]"
    if ($answer -match "^(?i:y|yes)$") {
        $pointer = [IntPtr]::Zero
        $plainKey = $null
        try {
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
            $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
            $headers = @{ Authorization = "Bearer $plainKey" }
            $body = @{
                model = $Model
                messages = @(
                    @{ role = "system"; content = "Return only a valid JSON object." },
                    @{ role = "user"; content = "Return this JSON object exactly: {`"status`":`"ok`"}" }
                )
                response_format = @{ type = "json_object" }
                enable_thinking = $false
                preserve_thinking = $false
            } | ConvertTo-Json -Depth 8 -Compress
            $response = Invoke-RestMethod `
                -Method Post `
                -Uri "$BaseUrl/chat/completions" `
                -Headers $headers `
                -ContentType "application/json; charset=utf-8" `
                -Body $body `
                -TimeoutSec 90
            $content = [string]$response.choices[0].message.content
            $parsedContent = $content | ConvertFrom-Json
            if ($parsedContent.status -ne "ok") {
                throw "The model returned an unexpected JSON payload."
            }
            Write-Host "Qwen connection test passed." -ForegroundColor Green
        } finally {
            $plainKey = $null
            if ($pointer -ne [IntPtr]::Zero) {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
            }
        }
    } else {
        Write-Host "Connection test skipped."
    }
}

Write-Host "Restart the demo services so the new LLM configuration is loaded." -ForegroundColor Yellow
