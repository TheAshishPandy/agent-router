$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Python = if ($env:AGENT_ROUTER_PYTHON) { $env:AGENT_ROUTER_PYTHON } else { "python" }
while ($true) {
  Write-Host "[$(Get-Date -Format s)] Starting repository orchestrator..."
  try { & $Python -m orchestrator.manager --config .\orchestrator-config.json; $exitCode=$LASTEXITCODE } catch { Write-Host $_; $exitCode=1 }
  if ($exitCode -eq 0) { break }
  Start-Sleep -Seconds 10
}
