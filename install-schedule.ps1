param([string]$TaskName = 'Japan Flight Tracker (Local)')
$ErrorActionPreference = 'Stop'
$trackerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $trackerPython)) { throw 'Install the local Python environment first.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task '$TaskName' already exists. Inspect it before replacing it."
}
$trackerRunner = Join-Path $PSScriptRoot 'run-tracker.ps1'
$trackerAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$trackerRunner`"" -WorkingDirectory $PSScriptRoot
# Omitting repetition duration means repeat indefinitely. Reset logic uses Chicago,
# independently of the Windows display timezone and daylight-saving changes.
$trackerTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 2)
$trackerLogon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$trackerSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$trackerPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $trackerAction -Trigger @($trackerTrigger, $trackerLogon) -Settings $trackerSettings -Principal $trackerPrincipal -Description 'DFW to Japan fare checks every 2 hours on this PC. Daily reset 08:00 America/Chicago. No flight purchases.'
