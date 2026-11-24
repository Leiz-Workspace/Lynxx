# Windows: run the standalone Lynxx gender model server (tools/build_model_server.py) in the background for this user, as a
# Scheduled Task started at logon and restarted if it fails. The desktop app calls http://127.0.0.1:8765.
#   powershell -ExecutionPolicy Bypass -File install-model-server.ps1 [-Exe C:\path\lynxx-model-server.exe] [-Port 8765]
#   powershell -ExecutionPolicy Bypass -File install-model-server.ps1 -Uninstall
# The console window is hidden with 'conhost.exe --headless' (Windows 10 1809 or later). Not tested by the maintainers yet.
param(
    [string]$Exe = (Join-Path $PSScriptRoot 'lynxx-model-server.exe'),
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$Name = 'Lynxx model server'
if ($Uninstall) {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "removed '$Name'"
    return
}
$Exe = (Resolve-Path -LiteralPath $Exe).Path
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" `
    -Argument "--headless `"$Exe`" --host 127.0.0.1 --port $Port --quiet" -WorkingDirectory (Split-Path -Parent $Exe)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Lynxx gender model server (HTTP API on 127.0.0.1)' -Force | Out-Null
Start-ScheduledTask -TaskName $Name
Write-Output "started '$Name' on 127.0.0.1:$Port"
