<#
.SYNOPSIS
    Installs the Local Code Operator as a hidden current-user logon task.
#>

param(
    [switch]$NoStart,
    [switch]$SkipTunnelDoctor
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $ProjectRoot

$PythonPath = (Get-Command python -ErrorAction Stop).Source
$TaskName = (& $PythonPath -m app.service task-name).Trim()
if (-not $TaskName) {
    throw "Configured service task name is empty."
}

Write-Host "[install-service] Running preflight checks..."
$DoctorArgs = @("-m", "app.service", "doctor")
if ($SkipTunnelDoctor) {
    $DoctorArgs += "--skip-tunnel-doctor"
}
& $PythonPath @DoctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Service preflight failed. Fix the failed doctor checks and retry."
}

$LegacyTasks = @(
    "gpt-local-code-operator-mcp",
    "gpt-local-code-operator-tunnel"
)
foreach ($LegacyTask in $LegacyTasks) {
    if (Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue) {
        Write-Host "[install-service] Removing legacy task: $LegacyTask"
        Stop-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false
    }
}

$HostScript = Join-Path $ProjectRoot "scripts\service-host.ps1"
$PwshPath = (Get-Command pwsh -ErrorAction Stop).Source
$QuotedHost = '"' + $HostScript + '"'
$QuotedPython = '"' + $PythonPath + '"'
$ActionArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File $QuotedHost -PythonPath $QuotedPython"
$ActionParameters = @{
    Execute = $PwshPath
    Argument = $ActionArguments
    WorkingDirectory = $ProjectRoot
}
$Action = New-ScheduledTaskAction @ActionParameters
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$SettingsParameters = @{
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable = $true
    RestartCount = 3
    RestartInterval = (New-TimeSpan -Minutes 1)
    ExecutionTimeLimit = [TimeSpan]::Zero
    MultipleInstances = "IgnoreNew"
}
$Settings = New-ScheduledTaskSettingsSet @SettingsParameters
$PrincipalParameters = @{
    UserId = "$env:USERDOMAIN\$env:USERNAME"
    LogonType = "Interactive"
    RunLevel = "Limited"
}
$Principal = New-ScheduledTaskPrincipal @PrincipalParameters
$RegisterParameters = @{
    TaskName = $TaskName
    Action = $Action
    Trigger = $Trigger
    Settings = $Settings
    Principal = $Principal
    Description = "User-level background supervisor for Local Code MCP and Secure MCP Tunnel"
    Force = $true
}
Register-ScheduledTask @RegisterParameters | Out-Null

Write-Host "[install-service] Installed task: $TaskName"
Write-Host "[install-service] Python: $PythonPath"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    $Deadline = [DateTime]::UtcNow.AddSeconds(130)
    do {
        Start-Sleep -Milliseconds 500
        $StatusOutput = & $PythonPath -m app.service status 2>$null
        if ($LASTEXITCODE -eq 0) {
            $ServiceStatus = $StatusOutput | ConvertFrom-Json
            if ($ServiceStatus.state -eq "healthy") {
                Write-Host "[install-service] Supervisor is healthy."
                exit 0
            }
        }
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Task was installed but the supervisor did not become observable before timeout."
}
