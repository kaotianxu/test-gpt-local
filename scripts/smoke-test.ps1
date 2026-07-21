<#
.SYNOPSIS
    Runs the live MCP contract smoke test.
.DESCRIPTION
    The default mode is read-only. It checks the local health endpoint,
    protected-resource discovery, MCP initialization, tool inventory,
    capabilities, ping, and project discovery.

    Use -Mutating together with -ProjectId to create and remove one isolated
    Git worktree as an extended lifecycle check.
#>

[CmdletBinding()]
param(
    [string]$McpUrl = "http://127.0.0.1:8765/mcp",
    [string]$ProjectId = "",
    [switch]$Mutating
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot

$PythonPath = (Get-Command python -ErrorAction Stop).Source
$PreviousMcpUrl = $env:SMOKE_MCP_URL
$PreviousProjectId = $env:SMOKE_PROJECT_ID
$PreviousMutating = $env:SMOKE_MUTATING
$ExitCode = 1

try {
    $env:SMOKE_MCP_URL = $McpUrl
    if ($ProjectId) {
        $env:SMOKE_PROJECT_ID = $ProjectId
    }
    else {
        Remove-Item Env:SMOKE_PROJECT_ID -ErrorAction SilentlyContinue
    }

    if ($Mutating) {
        $env:SMOKE_MUTATING = "1"
    }
    else {
        Remove-Item Env:SMOKE_MUTATING -ErrorAction SilentlyContinue
    }

    Write-Host "[smoke] Testing $McpUrl"
    if ($ProjectId) {
        Write-Host "[smoke] Project status target: $ProjectId"
    }
    if ($Mutating) {
        Write-Host "[smoke] Extended worktree lifecycle test: enabled"
    }

    & $PythonPath -m pytest -q tests/smoke -m smoke
    $ExitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $PreviousMcpUrl) {
        Remove-Item Env:SMOKE_MCP_URL -ErrorAction SilentlyContinue
    }
    else {
        $env:SMOKE_MCP_URL = $PreviousMcpUrl
    }
    if ($null -eq $PreviousProjectId) {
        Remove-Item Env:SMOKE_PROJECT_ID -ErrorAction SilentlyContinue
    }
    else {
        $env:SMOKE_PROJECT_ID = $PreviousProjectId
    }
    if ($null -eq $PreviousMutating) {
        Remove-Item Env:SMOKE_MUTATING -ErrorAction SilentlyContinue
    }
    else {
        $env:SMOKE_MUTATING = $PreviousMutating
    }
}

exit $ExitCode
