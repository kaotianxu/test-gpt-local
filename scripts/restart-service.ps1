<#
.SYNOPSIS
    Restarts the Local Code Operator background task.
#>

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $PSCommandPath
& (Join-Path $ScriptRoot "stop-service.ps1") -Force
& (Join-Path $ScriptRoot "start-service.ps1")
