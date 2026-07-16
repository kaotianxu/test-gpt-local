<#
.SYNOPSIS
    Stops and removes the Local Code Operator scheduled task.
#>

param(
    [switch]$RemoveTransientState
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot
$PythonPath = (Get-Command python -ErrorAction Stop).Source
$TaskName = (& $PythonPath -m app.service task-name).Trim()

& $PythonPath -m app.service stop --timeout 20 --force
if ($LASTEXITCODE -ne 0) {
    throw "Could not stop the owned supervisor process."
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

if ($RemoveTransientState) {
    $ServiceState = Join-Path $ProjectRoot "data\service"
    if (Test-Path -LiteralPath $ServiceState) {
        $ResolvedState = (Resolve-Path -LiteralPath $ServiceState).Path
        $ExpectedState = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "data\service"))
        if ($ResolvedState -ne $ExpectedState) {
            throw "Refusing to remove unexpected service state path: $ResolvedState"
        }
        Remove-Item -LiteralPath $ResolvedState -Recurse -Force
    }
}

Write-Host "[uninstall-service] Removed task: $TaskName"
Write-Host "[uninstall-service] Configuration, database, logs, and worktrees were preserved."
