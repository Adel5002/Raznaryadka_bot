# Starts the bot in the background (no window) under the supervisor, together with ngrok.
# The supervisor restarts the bot if it crashes or hangs.
#   Start: powershell -NoProfile -ExecutionPolicy Bypass -File start_bot_background.ps1
#   Stop:  powershell -NoProfile -ExecutionPolicy Bypass -File start_bot_background.ps1 -Stop
# Logs: data\logs\ (bot.log, errors.log, access.log, supervisor.log). PID: data\bot.pid
# Logs for the developer: collect_logs.bat (zip on the Desktop) or /logs in the bot.
param([switch]$Stop)

$root = $PSScriptRoot
$data = Join-Path $root "data"
$pidFile = Join-Path $data "bot.pid"
$py = Join-Path $root ".venv\Scripts\python.exe"

$running = $null
if (Test-Path $pidFile) {
    $old = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old) {
        $running = Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue
        if ($running -and $running.ProcessName -notlike "python*") { $running = $null }
    }
}

if ($Stop) {
    if ($running) {
        & taskkill.exe /PID $running.Id /T /F | Out-Null
        Write-Output "Bot stopped (PID $($running.Id))"
    } else {
        Write-Output "Bot is not running"
    }
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 0
}

if ($running) {
    Write-Output "Bot is already running (PID $($running.Id))"
    exit 0
}

if (-not (Test-Path $py)) {
    Write-Output "No venv: run run.bat once first"
    exit 1
}

$logs = Join-Path $data "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$p = Start-Process -FilePath $py -ArgumentList "-m", "bot.supervisor" -WorkingDirectory $root `
    -RedirectStandardOutput (Join-Path $logs "supervisor.out.log") `
    -RedirectStandardError (Join-Path $logs "supervisor.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $p.Id
Write-Output "Bot started under supervisor (PID $($p.Id)). Logs: $logs"
