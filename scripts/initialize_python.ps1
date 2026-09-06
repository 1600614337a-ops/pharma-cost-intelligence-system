param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [switch]$SkipDependencies
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$venvDir = Join-Path $ProjectRoot '.venv'
$pythonPath = Join-Path $venvDir 'Scripts\python.exe'
$originPath = Join-Path $venvDir '.environment-root'

function Test-PythonRuntime([string]$Executable) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable)) { return $false }
    try {
        & $Executable -c 'import sys,venv; sys.exit(0 if (3,11) <= sys.version_info < (4,0) else 1)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

$origin = if (Test-Path -LiteralPath $originPath) { (Get-Content -LiteralPath $originPath -Raw).Trim() } else { '' }
$reusable = $origin -eq $ProjectRoot -and (Test-PythonRuntime $pythonPath)
if (-not $reusable) {
    $systemPythonPath = $null
    foreach ($name in @('py', 'python', 'python3')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        try {
            $probeArgs = if ($name -eq 'py') { @('-3', '-c', 'import sys; print(sys.executable)') } else { @('-c', 'import sys; print(sys.executable)') }
            $candidate = & $command.Source @probeArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-PythonRuntime ([string](@($candidate)[-1])))) {
                $systemPythonPath = ([string](@($candidate)[-1])).Trim()
                break
            }
        } catch { }
    }
    if (-not $systemPythonPath) {
        throw '未找到可运行的 Python 3.11+。请安装 Python（建议 3.12，勾选 Add Python to PATH），或安装并启动 Docker Desktop 后使用“启动跨环境演示.cmd”。安装完成后重新双击启动文件。'
    }
    if (Test-Path -LiteralPath $venvDir) {
        # Preserve an old/moved environment; never recursively delete user files.
        $backup = Join-Path $ProjectRoot ('.venv.backup-' + [guid]::NewGuid().ToString('N'))
        Write-Host '检测到旧电脑、移动目录或失效的 Python 环境，备份后自动重建。'
        Move-Item -LiteralPath $venvDir -Destination $backup
    }
    Write-Host '首次运行：正在为当前电脑创建独立 Python 环境...'
    & $systemPythonPath -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-PythonRuntime $pythonPath)) { throw '创建 Python 环境失败，请检查安装是否完整、解压目录是否可写。' }
    Set-Content -LiteralPath $originPath -Value $ProjectRoot -Encoding UTF8 -NoNewline
}

if (-not $SkipDependencies) {
    $requirementsPath = Join-Path $ProjectRoot 'requirements.txt'
    $marker = Join-Path $venvDir '.requirements.sha256'
    $expected = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
    $installed = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { '' }
    $importsOk = $false
    try {
        & $pythonPath -c 'import pydantic,pypdf,docx,numpy,matplotlib,fastapi,uvicorn,httpx2,win32api,tzdata' 2>$null | Out-Null
        $importsOk = $LASTEXITCODE -eq 0
    } catch { }
    if ($installed -ne $expected -or -not $importsOk) {
        Write-Host '正在安装或修复依赖，请保持网络连接...'
        & $pythonPath -m pip install --disable-pip-version-check -r $requirementsPath | Out-Host
        if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络或代理后重新启动。' }
        & $pythonPath -c 'import pydantic,pypdf,docx,numpy,matplotlib,fastapi,uvicorn,httpx2,win32api,tzdata'
        if ($LASTEXITCODE -ne 0) { throw '依赖导入检查失败，请查看上方错误。' }
        Set-Content -LiteralPath $marker -Value $expected -Encoding UTF8 -NoNewline
    }
}
Write-Output $pythonPath
