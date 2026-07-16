<#
.SYNOPSIS
    Hidden Task Scheduler host for the Local Code Operator supervisor.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot

& $PythonPath -m app.service run
exit $LASTEXITCODE
