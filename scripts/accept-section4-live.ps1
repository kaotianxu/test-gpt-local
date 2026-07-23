<#
.SYNOPSIS
    Runs the opt-in live MCP acceptance for iteration-plan Section 4.
.DESCRIPTION
    Exercises all seven code-intelligence tools against a disposable Gpt-Local
    worktree. Successful worktrees are discarded; failed worktrees are retained.
#>

[CmdletBinding()]
param(
    [string]$McpUrl = "http://127.0.0.1:8765/mcp",
    [string]$ProjectId = "Gpt-Local"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot

$PythonPath = (Get-Command python -ErrorAction Stop).Source
$PreviousMcpUrl = $env:SMOKE_MCP_URL
$PreviousProjectId = $env:SMOKE_PROJECT_ID
$PreviousSection4Live = $env:SECTION4_LIVE
$ExitCode = 1

try {
    $env:SMOKE_MCP_URL = $McpUrl
    $env:SMOKE_PROJECT_ID = $ProjectId
    $env:SECTION4_LIVE = "1"

    Write-Host "[section4-live] MCP endpoint: $McpUrl"
    Write-Host "[section4-live] Project: $ProjectId"
    & $PythonPath -m pytest -q -s `
        tests/smoke/test_section4_code_intelligence_live.py `
        -m section4_live
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
    if ($null -eq $PreviousSection4Live) {
        Remove-Item Env:SECTION4_LIVE -ErrorAction SilentlyContinue
    }
    else {
        $env:SECTION4_LIVE = $PreviousSection4Live
    }
}

exit $ExitCode
