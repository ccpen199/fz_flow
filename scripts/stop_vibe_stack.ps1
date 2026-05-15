$ErrorActionPreference = "SilentlyContinue"

$Root = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $Root "runtime_data\pids"

function Stop-ManagedProcess {
  param([string]$PidFile)
  if (Test-Path $PidFile) {
    $pid = Get-Content $PidFile
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
  }
}

function Stop-PortListener {
  param([int]$Port)
  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($conn in $connections) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
  }
}

Stop-ManagedProcess (Join-Path $PidDir "vibe_backend.pid")
Stop-ManagedProcess (Join-Path $PidDir "vibe_frontend.pid")
Stop-PortListener -Port 18900
Stop-PortListener -Port 18901
Write-Host "stopped vibe stack"
