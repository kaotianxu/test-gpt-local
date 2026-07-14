<#
.SYNOPSIS
    Installs scheduled tasks for automatic startup of gpt-local-code-operator.
.DESCRIPTION
    Creates two Windows Task Scheduler tasks that run at user logon:
    1. Start MCP Server
    2. Start tunnel-client (waits for proxy + MCP Server)
.NOTES
    Run this from an elevated PowerShell session (Run as Administrator).
    Adjust the project root path if needed.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$StartMcpScript = "$ProjectRoot\scripts\start-mcp.ps1"
$StartTunnelScript = "$ProjectRoot\scripts\start-tunnel.ps1"

$TaskUser = "$env:USERDOMAIN\$env:USERNAME"

function New-LogonTask {
    param(
        [string]$TaskName,
        [string]$ScriptPath,
        [string]$Description
    )

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

    $trigger = New-ScheduledTaskTrigger -AtLogon -User $TaskUser

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    $principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force

    Write-Host "[install] Created task: $TaskName"
}

Write-Host "[install] Installing scheduled tasks for gpt-local-code-operator..."
Write-Host "[install] Project root: $ProjectRoot"
Write-Host "[install] User: $TaskUser"

New-LogonTask `
    -TaskName "gpt-local-code-operator-mcp" `
    -ScriptPath $StartMcpScript `
    -Description "Local Code MCP Server for GPT-powered code operations"

New-LogonTask `
    -TaskName "gpt-local-code-operator-tunnel" `
    -ScriptPath $StartTunnelScript `
    -Description "Secure MCP Tunnel client for gpt-local-code-operator"

Write-Host "[install] Done. Tasks will run at next logon."
Write-Host "[install] To test now, run:"
Write-Host "  Start-Job -FilePath `"$StartMcpScript`""
Write-Host "  Start-Job -FilePath `"$StartTunnelScript`""