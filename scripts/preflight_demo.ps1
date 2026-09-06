param(
    [int]$WebPort = 8080,
    [int]$RpaPort = 8090
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = & (Join-Path $PSScriptRoot "initialize_python.ps1") -ProjectRoot $projectRoot
$arguments = @(
    (Join-Path $PSScriptRoot "demo_preflight.py"),
    "--project-root", $projectRoot,
    "--web-port", [string]$WebPort,
    "--rpa-port", [string]$RpaPort,
    "--json-output", "tmp\demo_runtime\preflight.json"
)
& $pythonPath @arguments
exit $LASTEXITCODE
