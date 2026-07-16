<#
.SYNOPSIS
    Prints structured Local Code Operator background-service status.
#>

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot
$PythonPath = (Get-Command python -ErrorAction Stop).Source
& $PythonPath -m app.service status
exit $LASTEXITCODE
