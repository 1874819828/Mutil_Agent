# ============================================================
# Multi-Agent 项目一键启动脚本
# 启动: agent-assistant(8080)+ student-management 后端(8000)+ 前端(5173)
# 用法: 右键本文件 -> 使用 PowerShell 运行
#       或在终端执行: powershell -ExecutionPolicy Bypass -File .\start-all.ps1
# ============================================================
$ErrorActionPreference = 'Continue'

$PY = 'D:\develop_tools\Anaconda\python.exe'   # Python 3.12(项目依赖安装于此环境)

function Test-Port($port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Multi-Agent 项目启动器" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ---- agent-assistant(8080)----
if (Test-Port 8080) {
    Write-Host "[跳过] agent-assistant 已在 8080 运行" -ForegroundColor Yellow
} else {
    Write-Host "[启动] agent-assistant -> http://127.0.0.1:8080/console/" -ForegroundColor Green
    Start-Process -FilePath $PY -ArgumentList '-m', 'app' `
        -WorkingDirectory 'D:\AI_multi_agent\agent-assistant' -WindowStyle Hidden
}

# ---- student-management 后端(8000)----
if (Test-Port 8000) {
    Write-Host "[跳过] student-management 后端已在 8000 运行" -ForegroundColor Yellow
} else {
    Write-Host "[启动] student-management 后端 -> http://127.0.0.1:8000/docs" -ForegroundColor Green
    Start-Process -FilePath $PY -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
        -WorkingDirectory 'D:\AI_multi_agent\student-management\backend' -WindowStyle Hidden
}

# ---- student-management 前端(5173)----
if (Test-Port 5173) {
    Write-Host "[跳过] student-management 前端已在 5173 运行" -ForegroundColor Yellow
} else {
    Write-Host "[启动] student-management 前端 -> http://127.0.0.1:5173" -ForegroundColor Green
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm run dev' `
        -WorkingDirectory 'D:\AI_multi_agent\student-management\frontend' -WindowStyle Hidden
}

Write-Host ""
Write-Host "[验证] 等待 6 秒后探测..." -ForegroundColor Cyan
Start-Sleep -Seconds 6
foreach ($c in @(
    @{ name = 'agent-assistant /health'; url = 'http://127.0.0.1:8080/health' },
    @{ name = 'agent-assistant 控制台';    url = 'http://127.0.0.1:8080/console/' },
    @{ name = 'student-management 后端';   url = 'http://127.0.0.1:8000/' },
    @{ name = 'student-management 前端';   url = 'http://127.0.0.1:5173/' }
)) {
    try {
        $r = Invoke-WebRequest -Uri $c.url -UseBasicParsing -TimeoutSec 10
        Write-Host "  [OK]  $($c.name) => $($r.StatusCode)" -ForegroundColor Green
    } catch {
        Write-Host "  [FAIL] $($c.name)" -ForegroundColor Red
    }
}
Write-Host ""
Write-Host "完成。控制台: http://127.0.0.1:8080/console/" -ForegroundColor Cyan
Write-Host "控制令牌在 agent-assistant\runtime\control-token" -ForegroundColor DarkGray
