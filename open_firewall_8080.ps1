param(
  [int]$Port = 8080,
  [string]$RuleName = "CarmenKarla Local Python Server"
)

$ErrorActionPreference = 'Stop'

Write-Host "[CarmenKarla] Configuring firewall rule for TCP port $Port..."

try {
  $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
  if ($existing) {
    Write-Host "[CarmenKarla] Rule already exists: $RuleName"
    exit 0
  }

  New-NetFirewallRule `
    -DisplayName $RuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -Profile Any `
    -ErrorAction Stop | Out-Null

  Write-Host "[CarmenKarla] Firewall rule added successfully."
  exit 0
}
catch {
  Write-Host "[CarmenKarla] Could not add firewall rule automatically."
  Write-Host "Run PowerShell as Administrator then execute this script again."
  Write-Host $_.Exception.Message
  exit 1
}
