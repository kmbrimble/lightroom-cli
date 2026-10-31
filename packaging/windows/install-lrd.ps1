# install-lrd.ps1 - register (or re-register) the per-user LightroomCliDaemon task and start it.
# PowerShell 5.1 compatible, idempotent, no admin needed (task runs as the current user, LeastPrivilege).
# Run on the desktop:  powershell -NoProfile -ExecutionPolicy Bypass -File install-lrd.ps1
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'lightroom-cli'),
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$TaskName = 'LightroomCliDaemon'
$Py   = Join-Path $Root 'venv\Scripts\python.exe'
$PyW  = Join-Path $Root 'venv\Scripts\pythonw.exe'

if (-not (Test-Path $PyW)) { throw "pythonw.exe not found at $PyW - install the package into the venv first (uv pip install ...)" }
# PS 5.1: native stderr + 2>&1 under 'Stop' throws, so relax around native calls.
$ErrorActionPreference = 'Continue'
& $Py -c "import lightroom_sdk.daemon.main" 2>&1 | Out-Null
$importRc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($importRc -ne 0) { throw "lightroom_sdk.daemon is not importable in $Py - reinstall the package into the venv" }

# Stable copy of the thin client + .cmd shim (lrc must run as a file with python -I -S, never as a console_script).
$pkgDir = (& $Py -c "import lightroom_sdk,os;print(os.path.dirname(lightroom_sdk.__file__))").Trim()
Copy-Item -Force (Join-Path $pkgDir 'lrc.py') (Join-Path $Root 'lrc.py')
$shim = '@"%LOCALAPPDATA%\lightroom-cli\venv\Scripts\python.exe" -I -S "%LOCALAPPDATA%\lightroom-cli\lrc.py" %*'
Set-Content -Path (Join-Path $Root 'lrc.cmd') -Value $shim -Encoding ASCII

# Build the task XML from the template shipped next to this script.
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$tpl = Get-Content -Raw -Path (Join-Path $PSScriptRoot 'lrd-task.xml')
$xml = $tpl.Replace('@@SID@@', $sid).Replace('@@PYTHONW@@', $PyW).Replace('@@WORKDIR@@', $Root)

# Stop a running instance cleanly first (idempotent: ignore failure when not running).
$lrc = Join-Path $Root 'lrc.py'
$ErrorActionPreference = 'Continue'
& $Py -I -S $lrc stop 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'

try {
    Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
} catch {
    Write-Warning "Register-ScheduledTask failed ($($_.Exception.Message)); falling back to schtasks /create /xml"
    $tmp = Join-Path $env:TEMP 'lrd-task.xml'
    [System.IO.File]::WriteAllText($tmp, $xml, [System.Text.Encoding]::Unicode)   # UTF-16 LE, matches the XML header
    try {
        & schtasks.exe /create /tn $TaskName /xml $tmp /f
        if ($LASTEXITCODE -ne 0) { throw "schtasks /create failed with exit code $LASTEXITCODE" }
    } finally { Remove-Item -Force $tmp -ErrorAction SilentlyContinue }
}
Write-Host "Task $TaskName registered."

if ($NoStart) { return }
& schtasks.exe /run /tn $TaskName | Out-Null

# Verify: lrd.json appears and /v1/health answers (this talks to the DAEMON only, never to Lightroom).
$deadline = (Get-Date).AddSeconds(15)
$ok = $false
while ((Get-Date) -lt $deadline -and -not $ok) {
    Start-Sleep -Milliseconds 300
    $sf = Join-Path $Root 'lrd.json'
    if (-not (Test-Path $sf)) { continue }
    try {
        $st = Get-Content -Raw $sf | ConvertFrom-Json
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$($st.port)/v1/health")
        $req.Headers.Add('X-Lrd-Token', $st.token)
        $req.Timeout = 2000
        $resp = $req.GetResponse()
        $ok = ($resp.StatusCode -eq 200)
        $resp.Close()
    } catch { }
}
if ($ok) { Write-Host "lrd is up (see $Root\lrd.log). Try: $Root\lrc.cmd health" }
else { Write-Warning "lrd did not answer /v1/health within 15 s - check $Root\lrd.log"; exit 1 }
