# chat-smoke.ps1 — 阶段1验收：真实 /api/chat 三轮对话（路由 + 多轮上下文）
param([string]$JarPath = "target\smart-cs-agent-1.0.0.jar", [int]$Port = 18080)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 从仓库根 .env 读取 LLM 配置（不把 key 写进脚本）
$envFile = Join-Path $PSScriptRoot "..\.env"
foreach ($line in Get-Content $envFile -ErrorAction Stop) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)\s*$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
    }
}
if (-not $env:OPENAI_API_KEY) { Write-Output "FAIL: .env 缺少 OPENAI_API_KEY"; exit 1 }
# Spring AI 约定 base-url 不含 /v1（自动拼接 /v1/chat/completions）；
# Python SDK 约定 base-url 含 /v1 —— 读取 .env 后做规范化，不影响 Python 侧
if ($env:OPENAI_BASE_URL -match '/v1/?$') {
    $env:SPRING_AI_OPENAI_BASE_URL = $env:OPENAI_BASE_URL -replace '/v1/?$', ''
}
Write-Output "LLM: model=$env:MODEL_NAME base=$env:SPRING_AI_OPENAI_BASE_URL key=***$($env:OPENAI_API_KEY.Substring($env:OPENAI_API_KEY.Length-4))"

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

    $sid = "java-e2e-$(Get-Date -Format 'HHmmss')"
    $turns = @(
        @{ name = "T1 知识咨询"; msg = "金葵理财的收益率是多少？" },
        @{ name = "T2 多轮指代(依赖T1上下文)"; msg = "那这个产品最低要投多少钱？" },
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
    Write-Output "CHAT SMOKE DONE"
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Write-Output "process stopped"
}
