# 大云壁画工具箱 · 精卫 Jingwei —— 统一启动脚本
# 规范见 SERIES-SPEC v1.0 §6：定位 Python → 检查依赖 → 启动 → 健康检查 → 开浏览器
#
# 【重要】本文件必须保存为 UTF-8 **带 BOM**。
#   Windows PowerShell 5.1（run.bat 调用的就是它）在没有 BOM 时
#   会按 GBK 解码 .ps1，中文全部变乱码，而且会直接造成语法错误
#   ——脚本根本跑不起来。改这个文件之后务必确认 BOM 还在。
#
# 由 run.bat 双击调用，也可以直接在 PowerShell 里运行：.\run.ps1
#   .\run.ps1              正常启动，并自动打开浏览器
#   .\run.ps1 -NoBrowser   只起服务、不开浏览器（自动化测试用）

param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# 单独运行本脚本（不经过 run.bat）时也要把控制台切到 UTF-8
try { chcp 65001 | Out-Null } catch { }
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$ToolCN = "精卫"
$ToolEN = "Jingwei"
$Port = 8786
$AppUrl = "http://127.0.0.1:$Port/"
$HealthUrl = "http://127.0.0.1:$Port/api/health"

Write-Host ("=" * 44)
Write-Host ("  大云壁画工具箱 · {0} {1}" -f $ToolCN, $ToolEN)
Write-Host ("=" * 44)
Write-Host ("  地址：{0}" -f $AppUrl)
Write-Host "  关闭此窗口即停止工具。"
Write-Host ""

# ---------------------------------------------------------------- [1/4] Python
Write-Host "[1/4] 检查 Python ... " -NoNewline
$pythonExe = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
    $pythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
}

if (-not $pythonExe) {
    Write-Host "失败"
    Write-Host ""
    Write-Host "  没有找到 Python。请先安装 Python 3.9 或更高版本："
    Write-Host "    https://www.python.org/downloads/"
    Write-Host "  安装时务必勾选「Add python.exe to PATH」，然后重新双击 run.bat。"
    Write-Host ""
    exit 1
}

$verText = ""
try { $verText = (& $pythonExe @pythonArgs -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null | Select-Object -Last 1) } catch { }
$verOk = $false
if ($verText) {
    $parts = $verText.Trim().Split('.')
    if ($parts.Length -ge 2 -and [int]$parts[0] -eq 3 -and [int]$parts[1] -ge 9) { $verOk = $true }
}
if (-not $verOk) {
    Write-Host "失败"
    Write-Host ("  需要 Python 3.9 或更高版本（检测到「{0}」）。" -f $verText)
    exit 1
}
Write-Host ("OK   Python {0}" -f $verText.Trim())

# ------------------------------------------------- 已经在跑就不再启动第二个
try {
    $existing = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
    if ($existing.StatusCode -eq 200) {
        Write-Host ""
        Write-Host ("  检测到 {0} 已经在运行，直接打开页面。" -f $ToolCN)
        if (-not $NoBrowser) { Start-Process $AppUrl }
        exit 0
    }
} catch { }

# ---------------------------------------------------------------- [2/4] 依赖
Write-Host "[2/4] 检查依赖 ... " -NoNewline
& $pythonExe @pythonArgs -c "import cv2, numpy, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少依赖"
    Write-Host ""
    Write-Host ("  {0} 需要 Pillow、numpy、opencv-python 三个库。" -f $ToolCN)
    $answer = Read-Host "  现在自动安装吗？(Y/N)"
    if ($answer -match '^[Yy]') {
        Write-Host "  正在安装，第一次可能要几分钟…"
        & $pythonExe @pythonArgs -m pip install -r (Join-Path $Root "requirements.txt")
        & $pythonExe @pythonArgs -c "import cv2, numpy, PIL" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  依赖安装失败。请手动执行："
            Write-Host ("    {0} -m pip install -r requirements.txt" -f $pythonExe)
            exit 1
        }
        Write-Host "  依赖安装完成。"
    } else {
        Write-Host "  已取消。手动安装命令："
        Write-Host ("    {0} -m pip install -r requirements.txt" -f $pythonExe)
        exit 1
    }
} else {
    Write-Host "OK"
}

# ---------------------------------------------------------------- [3/4] 启动
Write-Host "[3/4] 启动服务 ... " -NoNewline
$serverArgs = @()
$serverArgs += $pythonArgs
$serverArgs += ('"{0}"' -f (Join-Path $Root "server.py"))
$serverArgs += "$Port"

$proc = Start-Process -FilePath $pythonExe -ArgumentList $serverArgs `
    -WorkingDirectory $Root -PassThru -NoNewWindow

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if ($proc.HasExited) { break }
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

if (-not $ready) {
    Write-Host "失败"
    Write-Host ""
    if ($proc.HasExited) {
        Write-Host ("  服务启动后立刻退出了（退出码 {0}），上面应有具体报错。" -f $proc.ExitCode)
    } else {
        Write-Host ("  等待 30 秒仍未就绪，端口 {0} 可能被别的程序占用。" -f $Port)
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host ""
    exit 1
}
Write-Host "OK"

# ---------------------------------------------------------------- [4/4] 浏览器
if ($NoBrowser) {
    Write-Host "[4/4] 跳过打开浏览器（-NoBrowser）"
} else {
    Write-Host "[4/4] 打开浏览器 ... " -NoNewline
    Start-Process $AppUrl
    Write-Host "OK"
}
Write-Host ""
Write-Host ("  {0} 正在运行。按 Ctrl+C 或直接关闭本窗口即可停止。" -f $ToolCN)
Write-Host ""

try { Wait-Process -Id $proc.Id } catch { }
Write-Host "  服务已停止。"
