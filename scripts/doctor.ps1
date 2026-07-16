<#
.SYNOPSIS
    Runs Local Code Operator installation and runtime diagnostics.
#>

param([switch]$SkipTunnelDoctor)

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot
$PythonPath = (Get-Command python -ErrorAction Stop).Source
$Arguments = @("-m", "app.service", "doctor")
if ($SkipTunnelDoctor) {
    $Arguments += "--skip-tunnel-doctor"
}
& $PythonPath @Arguments
exit $LASTEXITCODE
