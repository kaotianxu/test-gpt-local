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

$PythonPath = (Get-Command python -ErrorAction Stop).Source
$Runtime = (& $PythonPath -m app.service runtime-config) | ConvertFrom-Json
if ($Runtime.proxy.enabled) {
    $env:HTTP_PROXY = $Runtime.proxy.url
    $env:HTTPS_PROXY = $Runtime.proxy.url
    $env:ALL_PROXY = $Runtime.proxy.url
    $env:NO_PROXY = $Runtime.proxy.no_proxy -join ","
}

Write-Host "[start-mcp] Starting gpt-local-code-operator MCP Server..."
Write-Host "[start-mcp] Listening on http://127.0.0.1:8765/mcp"

& $PythonPath -m app.server
exit $LASTEXITCODE
