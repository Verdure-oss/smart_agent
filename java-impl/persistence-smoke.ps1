# persistence-smoke.ps1 — 阶段6验收：SQLite 持久化
# 流程：启动 -> 创建工单 -> 停机 -> 再次启动 -> 直查 SQLite 确认工单仍在
param([string]$JarPath = "target\smart-cs-agent-1.0.0.jar", [int]$Port = 18080)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$java = "E:\Program Files\Java\jdk-21\bin\java.exe"
$out = Join-Path $env:TEMP "smartcs-boot.log"
$err = Join-Path $env:TEMP "smartcs-boot.err.log"
$pgm = Join-Path $PSScriptRoot "data\smartcs.db"

# 清理旧 DB，确保本测试内的工单是自己创建的
if (Test-Path $pgm) { Remove-Item $pgm -Force }
Write-Output "DB cleaned: $pgm"

function Start-App {
    $p = Start-Process -FilePath $java -ArgumentList @("-jar", $JarPath, "--server.port=$Port") -PassThru -WindowStyle Hidden `
         -RedirectStandardOutput $out -RedirectStandardError $err
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if ($p.HasExited) { break }
        try {
            $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
            $req.Proxy = $null; $req.Timeout = 3000
            $resp = $req.GetResponse(); $resp.Close()
            return $p
        } catch {}
    }
    Write-Output "BOOT FAILED"; Get-Content $err -Tail 15 | ForEach-Object { "ERR| $_" }; exit 1
}

$occupied = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $occupied = $true; $c.Close() } catch {}
if ($occupied) { Write-Output "PORT $Port BUSY"; exit 2 }

Write-Output "=== 第一次启动 ==="
$p1 = Start-App
# 创建工单
$body = @{ name = "ticket_create"; arguments = @{ user_id = "user_p6"; description = "持久化验证工单：无法登录账户"; priority = "high" } } | ConvertTo-Json
$tmp = Join-Path $env:TEMP "p6-body.json"
[System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding($false)))
$r1 = & curl.exe --noproxy "*" -s -X POST -H "Content-Type: application/json" --data-binary "@$tmp" "http://127.0.0.1:$Port/api/tools/call"
Write-Output "创建工单响应: $r1"
# 提取 ticket_id
$tid = if ($r1 -match '"ticket_id":"([^"]+)"') { $Matches[1] } else { "" }
if (-not $tid) { Write-Output "FAIL: 未拿到 ticket_id"; Stop-Process -Id $p1.Id -Force; exit 1 }
Write-Output "ticket_id = $tid"
Stop-Process -Id $p1.Id -Force
Start-Sleep -Seconds 2
Write-Output "=== 第一次停止 ==="

Write-Output "=== 第二次启动（验证持久化）==="
$p2 = Start-App
# 直查 SQLite（Python 侧 sqlite3 或直接读文件？用 curl 查询工单接口验证）
$body2 = @{ name = "ticket_create_check"; arguments = @{} } | ConvertTo-Json
# 通过重建后的 order_query 难查工单，直接读 SQLite 文件
$python = "E:\miniconda3\envs\pytorchcuda\python.exe"
if (Test-Path $python) {
    $sql = "import sqlite3; c=sqlite3.connect(r'$pgm'); rows=c.execute('select ticket_id,user_id,priority,status from tickets order by created_at desc').fetchall(); print('DB_ROWS=' + str(rows))"
    $dbld = & $python -c $sql 2>&1
    Write-Output "SQLite 直查: $dbld"
    if ($dbld -match "DB_ROWS=.*$tid") {
        Write-Output "PERSIST OK: 工单 $tid 在重启后仍在 SQLite 中"
    } else {
        Write-Output "PERSIST FAIL: 重启后未找到工单"
    }
} else {
    Write-Output "跳过 Python 直查（python 不可用）"
}
Stop-Process -Id $p2.Id -Force
Write-Output "=== 第二次停止 ==="