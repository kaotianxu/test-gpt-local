<#
.SYNOPSIS
    Gracefully stops the installed Local Code Operator background task.
#>

param([switch]$Force)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot
$PythonPath = (Get-Command python -ErrorAction Stop).Source
$Arguments = @("-m", "app.service", "stop", "--timeout", "20")
if ($Force) {
    $Arguments += "--force"
}
& $PythonPath @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "Service stop failed. Retry with -Force if graceful shutdown timed out."
}
