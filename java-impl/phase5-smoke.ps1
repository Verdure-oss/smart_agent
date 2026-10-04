# phase5-smoke.ps1 — 阶段5验收：真实 /api/chat 三轮（验证 LLM 合规审查 + token 计量）
# 复用 chat-smoke.ps1 的启动/停机逻辑，额外在结束后调用 /metrics 验证 token 字段。
param([string]$JarPath = "target\smart-cs-agent-1.0.0.jar", [int]$Port = 18080)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$envFile = Join-Path $PSScriptRoot "..\.env"
foreach ($line in Get-Content $envFile -ErrorAction Stop) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)\s*$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
    }
}
if (-not $env:OPENAI_API_KEY) { Write-Output "FAIL: .env 缺少 OPENAI_API_KEY"; exit 1 }
if ($env:OPENAI_BASE_URL -match '/v1/?$') {
    $env:SPRING_AI_OPENAI_BASE_URL = $env:OPENAI_BASE_URL -replace '/v1/?$', ''
}
Write-Output "LLM: model=$env:MODEL_NAME base=$env:SPRING_AI_OPENAI_BASE_URL"

$java = "E:\Program Files\Java\jdk-21\bin\java.exe"
$out = Join-Path $env:TEMP "smartcs-boot.log"
$err = Join-Path $env:TEMP "smartcs-boot.err.log"

$occupied = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $occupied = $true; $c.Close() } catch {}
if ($occupied) { Write-Output "PORT $Port BUSY - skip"; exit 2 }

$p = Start-Process -FilePath $java -ArgumentList @("-jar", $JarPath, "--server.port=$Port") -PassThru -WindowStyle Hidden `
     -RedirectStandardOutput $out -RedirectStandardError $err
try {
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if ($p.HasExited) { break }
        try {
            $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
            $req.Proxy = $null; $req.Timeout = 3000
            $resp = $req.GetResponse(); $resp.Close()
            $ready = $true; break
        } catch {}
    }
    if (-not $ready) { Write-Output "BOOT FAILED"; Get-Content $err -Tail 20 | ForEach-Object { Write-Output "ERR| $_" }; exit 1 }
    Write-Output "HEALTH OK`n"

    $sid = "java-p5-$(Get-Date -Format 'HHmmss')"
    $turns = @(
        @{ name = "T1 知识咨询"; msg = "金葵理财的收益率是多少？" },
        @{ name = "T2 多轮指代"; msg = "那最低投多少钱？" },
        @{ name = "T3 工单办理"; msg = "帮我创建一个投诉工单，服务态度很差" }
    )
    foreach ($t in $turns) {
        $payload = @{ message = $t.msg; user_id = "user_001"; session_id = $sid } | ConvertTo-Json
        $tmp = Join-Path $env:TEMP "chat-body.json"
        [System.IO.File]::WriteAllText($tmp, $payload, (New-Object System.Text.UTF8Encoding($false)))
        Write-Output "=== $($t.name) ==="
        Write-Output "Q: $($t.msg)"
        $r = & curl.exe --noproxy "*" -s --max-time 180 -X POST -H "Content-Type: application/json" --data-binary "@$tmp" "http://localhost:$Port/api/chat"
        Write-Output "A: $r`n"
    }

    Write-Output "=== /metrics (token 计量验证) ==="
    $m = & curl.exe --noproxy "*" -s --max-time 10 "http://localhost:$Port/api/metrics"
    Write-Output $m
    Write-Output ""
    Write-Output "PHASE5 SMOKE DONE"
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Write-Output "process stopped"
}