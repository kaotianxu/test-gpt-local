<#
.SYNOPSIS
    Compatibility wrapper for the Phase 5 single-task installer.
.DESCRIPTION
    The old two-task layout has been replaced by one supervised background task.
#>

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $PSCommandPath
Write-Warning "install-scheduled-tasks.ps1 is deprecated; installing the Phase 5 supervisor."
& (Join-Path $ScriptRoot "install-service.ps1")
