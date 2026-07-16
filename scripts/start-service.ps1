<#
.SYNOPSIS
    Starts the installed Local Code Operator background task.
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot
$PythonPath = (Get-Command python -ErrorAction Stop).Source
$TaskName = (& $PythonPath -m app.service task-name).Trim()

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Service task is not installed. Run scripts\install-service.ps1 first."
}
Start-ScheduledTask -TaskName $TaskName
Write-Host "[start-service] Start requested for $TaskName."
