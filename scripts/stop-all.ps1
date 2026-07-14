<#
.SYNOPSIS
    Stops the gpt-local-code-operator MCP Server and tunnel-client.
.DESCRIPTION
    Finds and terminates Python processes running the MCP server
    and tunnel-client processes.
#>

$ErrorActionPreference = "Stop"

Write-Host "[stop-all] Stopping gpt-local-code-operator services..."

# Stop MCP Server (Python process running app.server)
$mcpProcesses = Get-Process -Name "python" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "app\.server" }

if ($mcpProcesses) {
    $mcpProcesses | Stop-Process -Force
    Write-Host "[stop-all] MCP Server stopped."
} else {
    Write-Host "[stop-all] No MCP Server process found."
}

# Stop tunnel-client
$tunnelProcesses = Get-Process -Name "tunnel-client" -ErrorAction SilentlyContinue
if ($tunnelProcesses) {
    $tunnelProcesses | Stop-Process -Force
    Write-Host "[stop-all] tunnel-client stopped."
} else {
    Write-Host "[stop-all] No tunnel-client process found."
}

Write-Host "[stop-all] All services stopped."