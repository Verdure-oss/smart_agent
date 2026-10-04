# boot-smoke.ps1 — 阶段验收用：启动 jar、探测 /health、确保清理进程
param(
    [string]$JarPath = "target\smart-cs-agent-1.0.0.jar",
    [int]$Port = 18080
)
$ErrorActionPreference = "Continue"
$java = "E:\Program Files\Java\jdk-21\bin\java.exe"
$out = Join-Path $env:TEMP "smartcs-boot.log"
$err = Join-Path $env:TEMP "smartcs-boot.err.log"

# 端口占用检查
$occupied = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $occupied = $true; $c.Close() } catch {}
if ($occupied) { Write-Output "PORT $Port BUSY - skip boot test"; exit 2 }

$p = Start-Process -FilePath $java -ArgumentList @("-jar", $JarPath, "--server.port=$Port") -PassThru -WindowStyle Hidden `
     -RedirectStandardOutput $out -RedirectStandardError $err
try {
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if ($p.HasExited) { break }
        try {
            $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
            $req.Proxy = $null
            $req.Timeout = 3000
            $resp = $req.GetResponse()
            $code = [int]$resp.StatusCode
            $body = (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
            $resp.Close()
            if ($code -eq 200) { $ok = $true; Write-Output "HEALTH: $body"; break }
        } catch { Write-Output "probe[$i] err: $($_.Exception.Message)" }
    }
    if (-not $ok) {
        Write-Output "BOOT FAILED (exited=$($p.HasExited))"
        Get-Content $err -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { Write-Output "ERR| $_" }
        Get-Content $out -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { Write-Output "OUT| $_" }
        exit 1
    }
    Write-Output "SMOKE OK"
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Write-Output "process stopped"
}
