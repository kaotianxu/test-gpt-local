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
$DoctorArgs = @("-m", "app.service", "doctor", "--skip-task-action-check")
if ($SkipTunnelDoctor) {
    $DoctorArgs += "--skip-tunnel-doctor"
}
& $PythonPath @DoctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Service preflight failed. Fix the failed doctor checks and retry."
}

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Write-Host "[install-service] Stopping the existing service before upgrading it..."
    & $PythonPath -m app.service stop --timeout 20 --force
    if ($LASTEXITCODE -ne 0) {
        throw "Existing service could not be stopped safely."
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
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

$PythonwPath = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
if (-not (Test-Path -LiteralPath $PythonwPath -PathType Leaf)) {
    throw "Windowless Python launcher not found next to python.exe: $PythonwPath"
}
$ActionArguments = "-m app.service run"
$ActionParameters = @{
    Execute = $PythonwPath
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
Write-Host "[install-service] Windowless Python: $PythonwPath"

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
