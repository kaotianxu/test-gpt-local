<#
.SYNOPSIS
    Starts the tunnel-client to connect the local MCP Server to OpenAI Secure MCP Tunnel.
.DESCRIPTION
    Waits for the local proxy and MCP Server to be ready, then starts tunnel-client.
    Loads CONTROL_PLANE_API_KEY from the project-root .env file.
.NOTES
    Run this from the project root directory.
    See plan section 9.3 for details.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ProjectRoot

$ProxyUrl = "http://127.0.0.1:7897"
$ProxyHost = "127.0.0.1"
$ProxyPort = 7897
$McpHealthUrl = "http://127.0.0.1:8765/healthz"

# ---- Load runtime API key from .env ----
$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*(?:export\s+)?CONTROL_PLANE_API_KEY\s*=\s*(.*?)\s*$') {
            $dotenvApiKey = $matches[1].Trim()
            if (($dotenvApiKey.Length -ge 2 -and $dotenvApiKey.StartsWith('"') -and $dotenvApiKey.EndsWith('"')) -or
                ($dotenvApiKey.Length -ge 2 -and $dotenvApiKey.StartsWith("'") -and $dotenvApiKey.EndsWith("'"))) {
                $dotenvApiKey = $dotenvApiKey.Substring(1, $dotenvApiKey.Length - 2)
            }
            if ($dotenvApiKey) {
                $env:CONTROL_PLANE_API_KEY = $dotenvApiKey
            }
            break
        }
    }
}

# ---- tunnel-client control plane explicitly goes through proxy ----
$env:CONTROL_PLANE_HTTP_PROXY = $ProxyUrl

# ---- Standard proxy variables for subprocesses ----
$env:HTTP_PROXY = $ProxyUrl
$env:HTTPS_PROXY = $ProxyUrl
$env:ALL_PROXY = $ProxyUrl
$env:NO_PROXY = "127.0.0.1,localhost,::1"

# ---- Runtime API key check ----
if (-not $env:CONTROL_PLANE_API_KEY) {
    throw "CONTROL_PLANE_API_KEY is not set. Generate it from OpenAI Platform → Tunnel Settings."
}

# ---- Wait for local proxy ----
Write-Host "[start-tunnel] Waiting for proxy at $ProxyUrl ..."
$proxyReady = $false
for ($i = 0; $i -lt 60; $i++) {
    if (Test-NetConnection -ComputerName $ProxyHost -Port $ProxyPort `
            -InformationLevel Quiet -WarningAction SilentlyContinue) {
        $proxyReady = $true
        break
    }
    Start-Sleep -Seconds 2
}

if (-not $proxyReady) {
    throw "Proxy is not reachable at $ProxyUrl after 120 seconds."
}
Write-Host "[start-tunnel] Proxy is ready."

# ---- Wait for local MCP Server ----
Write-Host "[start-tunnel] Waiting for MCP Server at $McpHealthUrl ..."
$mcpReady = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $response = Invoke-WebRequest `
            -Uri $McpHealthUrl `
            -UseBasicParsing `
            -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $mcpReady = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $mcpReady) {
    throw "Local MCP Server is not ready at $McpHealthUrl after 120 seconds."
}
Write-Host "[start-tunnel] MCP Server is ready."

# ---- Run diagnostic before connecting ----
Write-Host "[start-tunnel] Running tunnel-client doctor ..."
tunnel-client doctor --profile local-code-operator --explain
$doctorExitCode = $LASTEXITCODE
if ($doctorExitCode -ne 0) {
    throw "tunnel-client doctor failed with exit code $doctorExitCode. Fix the reported checks before starting the tunnel."
}

# ---- Start tunnel-client (long-running) ----
Write-Host "[start-tunnel] Starting tunnel-client ..."
tunnel-client run --profile local-code-operator
