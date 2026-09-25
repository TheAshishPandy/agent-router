# Agent Router continuous Windows runner
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Python = if ($env:AGENT_ROUTER_PYTHON) { $env:AGENT_ROUTER_PYTHON } else { "python" }
$HostAddr = if ($env:AGENT_ROUTER_HOST) { $env:AGENT_ROUTER_HOST } else { "127.0.0.1" }
$Port = if ($env:AGENT_ROUTER_PORT) { $env:AGENT_ROUTER_PORT } else { "8001" }
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Write-Host "Agent Router continuous runner"
Write-Host "Endpoint: http://$HostAddr`:$Port/api"
while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $log = Join-Path $LogDir "agent-router-$stamp.log"
    Write-Host "[$(Get-Date -Format s)] Starting Agent Router..."
    try {
        & $Python -m uvicorn main:app --host $HostAddr --port $Port --timeout-keep-alive 120 2>&1 | Tee-Object -FilePath $log
        $exitCode = $LASTEXITCODE
    } catch {
        $_ | Out-File -FilePath $log -Append
        $exitCode = 1
    }
    Write-Host "[$(Get-Date -Format s)] Agent Router stopped (exit=$exitCode). Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}
