# uninstall-lrd.ps1 - stop lrd and remove the LightroomCliDaemon task. Idempotent. PowerShell 5.1.
# Leaves %LOCALAPPDATA%\lightroom-cli (logs, audit.jsonl, venv) in place; pass -Purge to remove lrd state files only.
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'lightroom-cli'),
    [switch]$Purge
)
$ErrorActionPreference = 'Continue'
$TaskName = 'LightroomCliDaemon'
$Py  = Join-Path $Root 'venv\Scripts\python.exe'
$lrc = Join-Path $Root 'lrc.py'

if ((Test-Path $Py) -and (Test-Path $lrc)) { & $Py -I -S $lrc stop 2>&1 | Out-Null; Start-Sleep -Seconds 1 }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    & schtasks.exe /end /tn $TaskName 2>&1 | Out-Null
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task $TaskName removed."
} else { Write-Host "Task $TaskName was not registered." }

if ($Purge) {
    foreach ($f in 'lrd.json', 'lrd.lock', 'lrd.log', 'lrd.log.1', 'lrd.log.2', 'lrc.cmd', 'lrc.py') {
        Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Root $f)
    }
    Write-Host "Removed lrd state files (audit.jsonl kept)."
}
