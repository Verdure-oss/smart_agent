# checkpoint-otel-smoke.ps1 — 验收 MemorySaver 断点续接 + OTel span
param(
    [string]$JarPath = "target\smart-cs-agent-1.0.0.jar",
    [int]$Port = 18080
)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$java = "E:\Program Files\Java\jdk-21\bin\java.exe"
$out = Join-Path $env:TEMP "smartcs-boot-ckpt.log"
$err = Join-Path $env:TEMP "smartcs-boot-ckpt.err.log"

# 读 .env → Spring AI 环境变量
$envFile = Join-Path (Resolve-Path "..").Path ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Z0-9_]+)\s*=\s*(.+)\s*$') {
            $k = $matches[1]; $v = $matches[2].Trim('"',' ')
            if ($k -eq "OPENAI_API_KEY") { $env:SPRING_AI_OPENAI_API_KEY = $v }
            elseif ($k -eq "MODEL_NAME") { $env:SPRING_AI_OPENAI_CHAT_OPTIONS_MODEL = $v }
            elseif ($k -eq "OPENAI_BASE_URL") {
                $v = $v -replace '/v1$',''
                $env:SPRING_AI_OPENAI_BASE_URL = $v
            }
        }
    }
    Write-Output "LLM: model=$env:SPRING_AI_OPENAI_CHAT_OPTIONS_MODEL base=$env:SPRING_AI_OPENAI_BASE_URL"
}

$p = Start-Process -FilePath $java -ArgumentList @("-jar", $JarPath, "--server.port=$Port") -PassThru -WindowStyle Hidden `
     -RedirectStandardOutput $out -RedirectStandardError $err
try {
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if ($p.HasExited) { break }
        try {
            $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
            $req.Proxy = $null; $req.Timeout = 3000
            $resp = $req.GetResponse()
            if ([int]$resp.StatusCode -eq 200) { $ok = $true; Write-Output "HEALTH OK"; $resp.Close(); break }
            $resp.Close()
        } catch {}
    }
    if (-not $ok) { Write-Output "BOOT FAILED"; Get-Content $err -Tail 20 | ForEach-Object { $_ }; exit 1 }

    # 一轮真实对话（触发编排 → 生成 span + checkpoint）
    $body = @{ user_id = "user_p8"; message = "金葵理财的收益率是多少？" } | ConvertTo-Json
    $tmp = Join-Path $env:TEMP "ckpt-chat.json"
    $body | Out-File $tmp -Encoding UTF8
    $chatResp = curl.exe --noproxy "*" -s -X POST "http://127.0.0.1:$Port/api/chat" -H "Content-Type: application/json" --data-binary "@$tmp"
    $sessionId = ($chatResp | ConvertFrom-Json).session_id
    Write-Output "chat session_id = $sessionId"

    # 查 checkpoint
    Start-Sleep -Seconds 1
    $ckpt = curl.exe --noproxy "*" -s "http://127.0.0.1:$Port/api/checkpoint/$sessionId"
    Write-Output "checkpoint: $ckpt"
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Write-Output "process stopped"
    Write-Output "=== span traceId 日志（OTel）==="
    Select-String -Path $out -Pattern "traceId=" | Select-Object -First 5 | ForEach-Object { $_.Line }
    Write-Output "=== OTel 初始化 ==="
    Select-String -Path $out -Pattern "AgentTracer.*导出" | Select-Object -First 2 | ForEach-Object { $_.Line }
}