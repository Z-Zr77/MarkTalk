# 引擎工具 —— 供三个 .bat 调用
#   -Action start-freetoken  [模型目录]   启动 FreeToken 服务（8000）
#   -Action stop                          停止引擎（只杀 llama-server.exe / ft.exe，不误杀）
#   -Action status                        查看两个引擎在不在跑
param(
    [Parameter(Mandatory = $true)][string]$Action,
    [string]$ModelDir = ""
)

$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "config.json"

function Get-Config {
    if (Test-Path $ConfigPath) {
        try { return Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
    }
    return $null
}

function Get-PortOwner([int]$Port) {
    # 返回监听该端口的 (PID, 进程名)；没有则 $null
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $conn) { return $null }
    $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    return [pscustomobject]@{ PID = $conn.OwningProcess; Name = $(if ($p) { $p.ProcessName } else { "?" }) }
}

switch ($Action) {

    "status" {
        foreach ($x in @(@{P = 8080; N = "llama.cpp" }, @{P = 8000; N = "FreeToken" })) {
            $o = Get-PortOwner $x.P
            if ($o) { Write-Host ("  {0,-10} :{1}  运行中 (PID={2}, {3})" -f $x.N, $x.P, $o.PID, $o.Name) -ForegroundColor Green }
            else    { Write-Host ("  {0,-10} :{1}  未运行" -f $x.N, $x.P) -ForegroundColor DarkGray }
        }
        break
    }

    "stop" {
        $killed = 0
        # ★ 只结束明确是这两个引擎的进程，避免误杀占用同端口的其它程序
        foreach ($x in @(@{ P = 8080; EXE = "llama-server" }, @{ P = 8000; EXE = "ft" })) {
            $o = Get-PortOwner $x.P
            if ($o -and $o.Name -eq $x.EXE) {
                Write-Host ("  结束 {0} :{1}  PID={2}" -f $o.Name, $x.P, $o.PID) -ForegroundColor Yellow
                Stop-Process -Id $o.PID -Force -ErrorAction SilentlyContinue
                $killed++
            } elseif ($o) {
                Write-Host ("  端口 {0} 被 [{1}] 占用，不是本引擎，已跳过" -f $x.P, $o.Name) -ForegroundColor DarkYellow
            }
        }
        # 兜底：按进程名再清一次（llama.cpp 停止脚本原本就是这么做的）
        Get-Process -Name "llama-server" -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host ("  结束 llama-server.exe PID={0}" -f $_.Id) -ForegroundColor Yellow
            $_.Kill(); $killed++
        }
        if ($killed -eq 0) { Write-Host "  没有正在运行的引擎。" -ForegroundColor DarkGray }
        else { Write-Host ("  已停止 {0} 个进程，显存/内存已释放。" -f $killed) -ForegroundColor Green }
        break
    }

    "start-freetoken" {
        $cfg = Get-Config
        $modelsDir = if ($cfg -and $cfg.freetoken.models_dir) { $cfg.freetoken.models_dir } else { "" }
        # 未配置时留空，由后续检查报"未配置"错误，避免硬编码本机路径
        $ftExe     = if ($cfg -and $cfg.freetoken.ft_exe)     { $cfg.freetoken.ft_exe }     else { "" }
        # 未配置时留空，由后续检查报"未配置"错误，避免硬编码本机路径
        $serveArgs = if ($cfg -and $cfg.freetoken.serve_args) { @($cfg.freetoken.serve_args) } else { @("--host", "127.0.0.1", "--port", "8000", "--moe-strategy", "offload", "--mm-encoder-weights", "host") }

        if (-not (Test-Path $ftExe)) { Write-Host "  找不到 ft.exe：$ftExe" -ForegroundColor Red; exit 1 }

        # 已经在跑就提示
        $o = Get-PortOwner 8000
        if ($o) {
            Write-Host ("  端口 8000 已被 [{0}] 占用，FreeToken 可能已在运行。" -f $o.Name) -ForegroundColor Yellow
            exit 0
        }

        # 扫描可用模型（子目录里含 config.json 的才是模型）
        $models = @()
        if (Test-Path $modelsDir) {
            $models = Get-ChildItem $modelsDir -Directory -ErrorAction SilentlyContinue |
                      Where-Object { Test-Path (Join-Path $_.FullName "config.json") }
        }
        if ($models.Count -eq 0) { Write-Host "  $modelsDir 下没找到模型（需含 config.json）" -ForegroundColor Red; exit 1 }

        # 选模型：命令行给了就用，否则列菜单
        $pick = $null
        if ($ModelDir) {
            $pick = $models | Where-Object { $_.FullName -eq $ModelDir -or $_.Name -eq $ModelDir } | Select-Object -First 1
            if (-not $pick) { Write-Host "  指定的模型不存在：$ModelDir" -ForegroundColor Red; exit 1 }
        } elseif ($models.Count -eq 1) {
            $pick = $models[0]
        } else {
            Write-Host "  可用模型：" -ForegroundColor Cyan
            for ($i = 0; $i -lt $models.Count; $i++) { Write-Host ("    [{0}] {1}" -f ($i + 1), $models[$i].Name) }
            $sel = Read-Host "  请输入序号（回车默认 1）"
            if (-not $sel) { $sel = "1" }
            $idx = 0
            if (-not [int]::TryParse($sel, [ref]$idx) -or $idx -lt 1 -or $idx -gt $models.Count) {
                Write-Host "  序号无效。" -ForegroundColor Red; exit 1
            }
            $pick = $models[$idx - 1]
        }

        Write-Host ""
        Write-Host ("  模型 : {0}" -f $pick.Name) -ForegroundColor Green
        Write-Host ("  端口 : 8000") -ForegroundColor Green
        Write-Host ("  参数 : --moe-strategy {0}" -f ($(if ($serveArgs -contains "offload") { "offload（省内存）" } else { "hybrid（更快但更吃内存）" }))) -ForegroundColor Green
        Write-Host "  首次加载模型需要几分钟，请耐心等待…" -ForegroundColor Yellow
        Write-Host ""

        $args = @("serve", "--model-path", $pick.FullName) + $serveArgs + @("--served-model-name", $pick.Name)
        & $ftExe @args
        break
    }

    default { Write-Host "  未知动作：$Action" -ForegroundColor Red }
}
