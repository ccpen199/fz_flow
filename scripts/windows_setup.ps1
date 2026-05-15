# Run in Windows PowerShell as Administrator

$ErrorActionPreference = "Stop"

Write-Host "Installing OpenSSH Server..."
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null

Write-Host "Starting and enabling sshd..."
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

Write-Host "Ensuring firewall rule for TCP/22..."
if (-not (Get-NetFirewallRule -Name "sshd" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name "sshd" -DisplayName "OpenSSH Server" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

Write-Host "Current sshd status:"
Get-Service sshd | Format-Table -AutoSize

Write-Host "If WSL is not installed yet, run:"
Write-Host "  wsl --install -d Ubuntu"

