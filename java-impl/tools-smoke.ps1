# tools-smoke.ps1 — 阶段4验收：/api/tools/call 四工具真实逻辑 + /api/tools 发现
param([string]$JarPath = "target\smart-cs-agent-1.0.0.jar", [int]$Port = 18080)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$java = "E:\Program Files\Java\jdk-21\bin\java.exe"
$out = Join-Path $env:TEMP "smartcs-boot-tools.log"
$err = Join-Path $env:TEMP "smartcs-boot-tools.err.log"

$occupied = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $occupied = $true; $c.Close() } catch {}
if ($occupied) { Write-Output "PORT $Port BUSY - skip"; exit 2 }

function PostJson($url, $obj) {
    $tmp = Join-Path $env:TEMP ("req-" + [guid]::NewGuid().ToString("N") + ".json")
    [System.IO.File]::WriteAllText($tmp, ($obj | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
    try { return & curl.exe --noproxy "*" -s --max-time 60 -X POST -H "Content-Type: application/json" --data-binary "@$tmp" $url }
    finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
}
function GetJson($url) {
    return & curl.exe --noproxy "*" -s --max-time 30 $url
}

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
            $resp = $req.GetResponse(); $resp.Close(); $ready = $true; break
        } catch {}
    }
    if (-not $ready) { Write-Output "BOOT FAILED"; Get-Content $err -Tail 30 | ForEach-Object { Write-Output "ERR| $_" }; exit 1 }
    Write-Output "HEALTH OK`n"

    Write-Output "=== 1. tools/list 发现 ==="
    $tools = GetJson "http://localhost:$Port/api/tools"
    Write-Output $tools

    Write-Output "`n=== 2. order_query (按订单ID) ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "order_query"; arguments = @{ order_id = "ORD-20261001-001" } })

    Write-Output "`n=== 3. order_query (按用户，验证列表) ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "order_query"; arguments = @{ user_id = "user_001" } })

    Write-Output "`n=== 4. ticket_create (真实落库) ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "ticket_create"; arguments = @{ user_id = "user_001"; description = "测试工单：服务态度问题"; priority = "medium" } })

    Write-Output "`n=== 5. risk_check (大额退款 -> high) ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "risk_check"; arguments = @{ user_id = "user_001"; action = "refund"; amount = 8000 } })

    Write-Output "`n=== 6. risk_check (小额 -> low) ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "risk_check"; arguments = @{ user_id = "user_001"; action = "query"; amount = 50 } })

    Write-Output "`n=== 7. knowledge_search (混合召回) ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "knowledge_search"; arguments = @{ query = "金葵理财收益率"; top_k = 2 } })

    Write-Output "`n=== 8. 不存在的工具 -> 报错 ==="
    Write-Output (PostJson "http://localhost:$Port/api/tools/call" @{ name = "no_such_tool"; arguments = @{ } })

    Write-Output "`nTOOLS SMOKE DONE"
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Write-Output "process stopped"
}