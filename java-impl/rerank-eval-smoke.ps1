# rerank-eval-smoke.ps1 — 阶段7补：跑到 rerank 的完整链路评测（离线 IR 指标 + 导出 RAGAS dump）
param(
    [string]$JarPath = "target\smart-cs-agent-1.0.0.jar",
    [int]$Port = 18080
)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 从仓库根 .env 读取 LLM 配置（online LLM rerank 需要 key/base/model）
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
$out = Join-Path $env:TEMP "smartcs-rerank.log"
$err = Join-Path $env:TEMP "smartcs-rerank.err.log"

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
    if (-not $ready) { Write-Output "BOOT FAILED"; exit 1 }
    Write-Output "HEALTH OK"

    $url = "http://127.0.0.1:$Port/api/eval/rerank?dataset=..%5Ceval%5Cdataset_v2.json&out=..%5Ceval%5Cjava_retrievals_rerank.json"
    Write-Output "触发评测(24 次 LLM rerank，约 2-5 分钟)..."
    $r = & curl.exe --noproxy "*" -s --max-time 600 $url
    Write-Output "评测结果:"
    Write-Output $r
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Write-Output "process stopped"
}