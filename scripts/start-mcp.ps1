<#
.SYNOPSIS
    Starts the gpt-local-code-operator MCP Server.
.DESCRIPTION
    Launches the FastMCP server on 127.0.0.1:8765 with Streamable HTTP transport.
    Sets proxy environment variables for subprocesses.
.NOTES
    Run this from the project root directory.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ProjectRoot

# ---- Proxy environment for subprocesses ----
$ProxyUrl = "http://127.0.0.1:7897"

$env:HTTP_PROXY = $ProxyUrl
$env:HTTPS_PROXY = $ProxyUrl
$env:ALL_PROXY = $ProxyUrl
$env:NO_PROXY = "127.0.0.1,localhost,::1"

Write-Host "[start-mcp] Starting gpt-local-code-operator MCP Server..."
Write-Host "[start-mcp] Listening on http://127.0.0.1:8765/mcp"

python -m app.server