param(
  [string]$BackendHost = $(if ($env:BACKEND_HOST) { $env:BACKEND_HOST } else { "127.0.0.1" }),
  [int]$BackendPort = $(if ($env:BACKEND_PORT) { [int]$env:BACKEND_PORT } else { 18900 }),
  [string]$FrontendHost = $(if ($env:FRONTEND_HOST) { $env:FRONTEND_HOST } else { "127.0.0.1" }),
  [int]$FrontendPort = $(if ($env:FRONTEND_PORT) { [int]$env:FRONTEND_PORT } else { 18901 }),
  [string]$EnvFile = $(if ($env:ENV_FILE) { $env:ENV_FILE } else { "" })
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "runtime_data\logs"
$PidDir = Join-Path $Root "runtime_data\pids"

if (-not $EnvFile) {
  $EnvFile = Join-Path $Root "deploy\fz-workflow-demo.app.env"
}

New-Item -ItemType Directory -Path $LogDir, $PidDir -Force | Out-Null

function Import-DotEnv {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return
  }

  Get-Content -LiteralPath $Path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") {
      return
    }
    $parts = $line -split "=", 2
    $key = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"').Trim("'")
    [Environment]::SetEnvironmentVariable($key, $value, "Process")
  }
}

function Stop-ManagedProcess {
  param([string]$PidFile)
  if (Test-Path -LiteralPath $PidFile) {
    Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_ -match "^\d+$") {
        Stop-Process -Id ([int]$_) -Force -ErrorAction SilentlyContinue
      }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  }
}

function Stop-PortListener {
  param([int]$Port)
  Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
  }
}

function Wait-Port {
  param(
    [int]$Port,
    [string]$Name,
    [int]$TimeoutSeconds = 40
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
      return $listener.OwningProcess
    }
    Start-Sleep -Milliseconds 250
  }
  throw "$Name failed to start on port $Port"
}

function Show-LogTail {
  param(
    [string]$Path,
    [string]$Label,
    [int]$Lines = 80
  )
  if (Test-Path -LiteralPath $Path) {
    Write-Host ""
    Write-Host "$Label ($Path)"
    Get-Content -LiteralPath $Path -Tail $Lines
  }
}

Import-DotEnv -Path $EnvFile

$env:VIBE_PERSIST_STATE = if ($env:VIBE_PERSIST_STATE) { $env:VIBE_PERSIST_STATE } else { "1" }
$env:VIBE_PPTX_EXPORTER = if ($env:VIBE_PPTX_EXPORTER) { $env:VIBE_PPTX_EXPORTER } else { "python-pptx" }
$env:PRESENTON_BASE_URL = if ($env:PRESENTON_BASE_URL) { $env:PRESENTON_BASE_URL } else { "http://127.0.0.1:15001" }
$env:PRESENTON_APP_DATA_DIR = if ($env:PRESENTON_APP_DATA_DIR) { $env:PRESENTON_APP_DATA_DIR } else { Join-Path $Root "runtime_data\presenton\app_data" }
$env:VIBE_PRESENTON_ALLOW_FALLBACK = if ($env:VIBE_PRESENTON_ALLOW_FALLBACK) { $env:VIBE_PRESENTON_ALLOW_FALLBACK } else { "0" }
$env:PRESENTON_DIRECT_DATA_SLIDES = if ($env:PRESENTON_DIRECT_DATA_SLIDES) { $env:PRESENTON_DIRECT_DATA_SLIDES } else { "1" }
$env:PRESENTON_APPEND_DATA_SLIDES = if ($env:PRESENTON_APPEND_DATA_SLIDES) { $env:PRESENTON_APPEND_DATA_SLIDES } else { "0" }

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
  $Python = "python"
}

Stop-ManagedProcess (Join-Path $PidDir "vibe_backend.pid")
Stop-ManagedProcess (Join-Path $PidDir "vibe_backend.launcher.pid")
Stop-ManagedProcess (Join-Path $PidDir "vibe_frontend.pid")
Stop-PortListener -Port $BackendPort
Stop-PortListener -Port $FrontendPort

$backendOut = Join-Path $LogDir "vibe_backend.out.log"
$backendErr = Join-Path $LogDir "vibe_backend.err.log"
$frontendOut = Join-Path $LogDir "vibe_frontend.out.log"
$frontendErr = Join-Path $LogDir "vibe_frontend.err.log"

$backend = Start-Process `
  -FilePath $Python `
  -ArgumentList @("-m", "uvicorn", "apps.vibe_backend.app.main:app", "--host", $BackendHost, "--port", "$BackendPort") `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $backendOut `
  -RedirectStandardError $backendErr `
  -PassThru

Set-Content -Path (Join-Path $PidDir "vibe_backend.launcher.pid") -Value $backend.Id
try {
  $backendPid = Wait-Port -Port $BackendPort -Name "vibe_backend"
} catch {
  Write-Host "vibe_backend startup failed."
  Show-LogTail -Path $backendErr -Label "backend stderr"
  Show-LogTail -Path $backendOut -Label "backend stdout"
  throw
}
Set-Content -Path (Join-Path $PidDir "vibe_backend.pid") -Value $backendPid

$frontend = Start-Process `
  -FilePath $Python `
  -ArgumentList @("-m", "http.server", "$FrontendPort", "--bind", $FrontendHost, "--directory", "apps/vibe_frontend/static") `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $frontendOut `
  -RedirectStandardError $frontendErr `
  -PassThru

try {
  $frontendPid = Wait-Port -Port $FrontendPort -Name "vibe_frontend"
} catch {
  Write-Host "vibe_frontend startup failed."
  Show-LogTail -Path $frontendErr -Label "frontend stderr"
  Show-LogTail -Path $frontendOut -Label "frontend stdout"
  throw
}
Set-Content -Path (Join-Path $PidDir "vibe_frontend.pid") -Value $frontendPid

Write-Host "Backend:  http://$BackendHost`:$BackendPort"
Write-Host "Frontend: http://$FrontendHost`:$FrontendPort"
Write-Host "Logs:     $LogDir"
