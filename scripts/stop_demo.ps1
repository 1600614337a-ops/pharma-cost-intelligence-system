param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$statePath = Join-Path $projectRoot "tmp\demo_runtime\processes.json"
if (-not (Test-Path -LiteralPath $statePath)) {
    Write-Host "No demo runtime state was found."
    exit 0
}

$state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
$targets = @()
if ($state.web_pid) {
    $targets += @{ Name = "Web"; Id = [int]$state.web_pid; Marker = "app.dashboard" }
}
if ($state.rpa_pid) {
    $targets += @{ Name = "Mock RPA"; Id = [int]$state.rpa_pid; Marker = "run_mock_rpa_local.py" }
}
foreach ($target in $targets) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.Id)" -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Host "$($target.Name) process is already stopped."
        continue
    }
    if (-not $process.CommandLine -or $process.CommandLine -notlike "*$($target.Marker)*") {
        Write-Warning "PID $($target.Id) does not match the expected $($target.Name) command and was not stopped."
        continue
    }
    Stop-Process -Id $target.Id -Force
    Write-Host "Stopped $($target.Name) (PID $($target.Id))."
}

$state.status = "stopped"
$state | Add-Member -NotePropertyName stopped_at -NotePropertyValue (Get-Date).ToString("o") -Force
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Host "Demo stop operation completed."
