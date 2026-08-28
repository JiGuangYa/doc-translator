# doc-translator one-click management script (Windows PowerShell, compatible with 5.1+)
# Usage: .\manage.ps1 {install|start|stop|restart|status|update}
# Or run manage.bat directly (auto-bypasses the execution policy).
# Features: idempotent (safe to re-run), PID file + health check, graceful stop, safe update.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---------- Constants (kept in sync with app/config.py) ----------
$BaseDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $BaseDir

$Host_       = '127.0.0.1'
$Port        = '8765'
$VenvDir     = Join-Path $BaseDir '.venv'
$Py          = Join-Path $VenvDir 'Scripts\python.exe'
$PidFile     = Join-Path $BaseDir 'data\server.pid'
$LogFile     = Join-Path $BaseDir 'data\server.log'
$HealthUrl   = "http://${Host_}:$Port/"
$StartTimeoutSec = 40
$StopTimeoutSec  = 15

# ---------- Output helpers ----------
function Info([string]$m) { Write-Host "[info] $m" }
function Ok([string]$m)   { Write-Host "[ok] $m" }
function Warn([string]$m) { Write-Warning $m }
function Die([string]$m)  { Write-Host "[error] $m" -ForegroundColor Red; exit 1 }

# ---------- Basic detection ----------

function Find-SystemPython {
    foreach ($cand in @('py', 'python')) {
        try {
            $v = & $cand -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>$null
            if ("$v".Trim() -eq '1') { return $cand }
        } catch { }
    }
    return $null
}

function Test-VenvReady {
    if (-not (Test-Path $Py)) { return $false }
    $out = & $Py -c "import fastapi, uvicorn; print('ok')" 2>$null
    return ("$out".Trim() -eq 'ok')
}

function Ensure-Installed {
    # Idempotent install: skip if the venv is healthy; otherwise (re)create it.
    if (Test-VenvReady) { return }

    Info "Starting install (virtual environment is missing or incomplete)..."
    $sysPy = Find-SystemPython
    if (-not $sysPy) {
        Die "Python 3.10+ not found. Install it and check 'Add to PATH': https://www.python.org/downloads/"
    }
    Info "Using system Python: $sysPy"
    $partial = "$VenvDir.partial"
    if (Test-Path $partial) { Remove-Item -Recurse -Force $partial }
    & $sysPy -m venv $partial
    if ($LASTEXITCODE -ne 0) { Die "Failed to create virtual environment" }
    # Build the venv in a temp dir first, then rename — avoids leaving a half-built venv behind.
    if (Test-Path $VenvDir) { Remove-Item -Recurse -Force $VenvDir }
    Rename-Item $partial (Split-Path -Leaf $VenvDir)

    Info "Installing dependencies (first run takes about 1-2 minutes)..."
    & $Py -m pip install --upgrade pip -q --disable-pip-version-check
    & $Py -m pip install -r (Join-Path $BaseDir 'requirements.txt') -q --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Die "Dependency install failed; please check your network and retry" }
    if (-not (Test-VenvReady)) { Die "Post-install self-check failed (fastapi/uvicorn import error)" }
    Ok "Install complete"
}

function Test-PortResponds {
    try {
        Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-StoredPid {
    if (Test-Path $PidFile) {
        try { return [int](Get-Content $PidFile -ErrorAction Stop) } catch { return $null }
    }
    return $null
}

function Test-PidIsServer([int]$procId) {
    # PID exists and the process belongs to this deployment's uvicorn (guards against
    # the PID being recycled by an unrelated process).
    try {
        $proc = Get-Process -Id $procId -ErrorAction Stop
    } catch { return $false }
    $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
    return ($cl -and $cl -like '*uvicorn*app.main:app*')
}

function Test-ServerRunning {
    if (Test-PortResponds) { return $true }
    $p = Get-StoredPid
    return ($null -ne $p -and (Test-PidIsServer $p))
}

function Wait-Health {
    for ($i = 0; $i -lt $StartTimeoutSec; $i++) {
        if (Test-PortResponds) { return $true }
        Start-Sleep -Seconds 1
        # Process already exited and the port is not up -> startup failed; stop waiting early.
        $cpid = Get-StoredPid
        if ($null -ne $cpid -and -not (Get-Process -Id $cpid -ErrorAction SilentlyContinue)) {
            return $false
        }
    }
    return (Test-PortResponds)
}

# ---------- Subcommands ----------

function Cmd-Install {
    Ensure-Installed
    New-Item -ItemType Directory -Force -Path (Join-Path $BaseDir 'data') | Out-Null
    Ok "Ready. Start the service: .\manage.ps1 start"
}

function Cmd-Start {
    if (Test-ServerRunning) {
        Ok "Service is already running (http://${Host_}:$Port); no need to start again"
        return
    }
    Ensure-Installed
    New-Item -ItemType Directory -Force -Path (Join-Path $BaseDir 'data') | Out-Null
    if (Test-Path $PidFile) { Remove-Item -Force $PidFile }

    Info "Starting service..."
    # Note: the child process keeps the log file handle open for its entire lifetime,
    # so we cannot redirect to a temp file and rename. Write to a fixed file directly;
    # stdout and stderr need separate files (Start-Process limitation).
    $proc = Start-Process -FilePath $Py `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', $Host_, '--port', $Port `
        -WorkingDirectory $BaseDir -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" -PassThru
    Set-Content -Path $PidFile -Value $proc.Id

    if (Wait-Health) {
        Ok "Service started (PID $($proc.Id)): http://${Host_}:$Port"
        try { Start-Process "http://${Host_}:$Port/" } catch { }
    } else {
        Warn "Service did not become ready within ${StartTimeoutSec}s. Recent log:"
        foreach ($f in @($LogFile, "$LogFile.err")) {
            if (Test-Path $f) { Get-Content $f -Tail 20 | ForEach-Object { Write-Host $_ } }
        }
        if (Test-Path $PidFile) { Remove-Item -Force $PidFile }
        exit 1
    }
}

function Cmd-Stop {
    $stopped = $false
    $storedPid = Get-StoredPid

    if ($null -ne $storedPid -and (Test-PidIsServer $storedPid)) {
        Info "Stopping service (PID $storedPid) gracefully..."
        # Ask Python to exit via its own SIGTERM handler; the FastAPI
        # lifespan will drain in-flight translation tasks (up to 30 s)
        # before letting the process die. Only escalate to /T /F if
        # the graceful window expires.
        Stop-Process -Id $storedPid -ErrorAction SilentlyContinue
        $waited = 0
        while ((Get-Process -Id $storedPid -ErrorAction SilentlyContinue) -and ($waited -lt $StopTimeoutSec)) {
            Start-Sleep -Seconds 1
            $waited += 1
        }
        if (Get-Process -Id $storedPid -ErrorAction SilentlyContinue) {
            Warn "Graceful stop timed out after ${StopTimeoutSec}s; force-killing process tree..."
            & taskkill /PID $storedPid /T /F *> $null
        }
        $stopped = $true
    }

    # Fallback: PID file is gone but this deployment's process is still listening —
    # match by command line.
    if (-not $stopped -and (Test-PortResponds)) {
        Warn "PID file missing but port $Port is responding. Cleaning up by command-line match..."
        Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
            Where-Object { $_.CommandLine -like "*$BaseDir*" -and $_.CommandLine -like '*uvicorn*app.main:app*' } |
            ForEach-Object {
                & taskkill /PID $_.ProcessId /T /F *> $null
                $stopped = $true
            }
    }

    if (Test-Path $PidFile) { Remove-Item -Force $PidFile }
    if ($stopped) { Ok "Service stopped" } else { Ok "Service is not running" }
}

function Cmd-Restart {
    Cmd-Stop
    Cmd-Start
}

function Cmd-Status {
    if (Test-ServerRunning) {
        $p = Get-StoredPid
        if ($null -eq $p) { $p = 'unknown' }
        Ok "running (PID $p): http://${Host_}:$Port"
    } else {
        Info "not running"
        exit 3   # Convention: exit code 3 means "service not running" (for scripted probes).
    }
}

function Cmd-Update {
    if (-not (Test-Path (Join-Path $BaseDir '.git'))) {
        Die "Not a git repository; cannot auto-update. Please download the new version manually."
    }

    $wasRunning = Test-ServerRunning

    Info "Pulling latest code..."
    $branch = (& git symbolic-ref --quiet --short HEAD) 2>$null
    if (-not $branch) { $branch = 'main' }
    & git fetch origin --prune
    if ($LASTEXITCODE -ne 0) { Die "git fetch failed; please check your network" }

    # Stash any local changes first (protect user edits) and restore them after the update.
    $stashed = $false
    git diff --quiet *> $null; $dirtyWorktree = ($LASTEXITCODE -ne 0)
    git diff --cached --quiet *> $null; $dirtyIndex = ($LASTEXITCODE -ne 0)
    if ($dirtyWorktree -or $dirtyIndex) {
        Info "Uncommitted local changes detected; stashing for safety..."
        & git stash push -u -m "manage.ps1 update $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        if ($LASTEXITCODE -ne 0) { Die "Failed to stash local changes; aborting update" }
        $stashed = $true
    }

    & git rev-parse --verify -q "origin/$branch" *> $null
    if ($LASTEXITCODE -eq 0) {
        & git merge --ff-only "origin/$branch"
        if ($LASTEXITCODE -ne 0) {
            Warn "Cannot fast-forward merge (local branch has diverged from remote)"
            if ($stashed) {
                & git stash pop 2>$null
                if ($LASTEXITCODE -ne 0) { Warn "Stash restore had conflicts; changes remain in the stash" }
            }
            Die "Please resolve the branch divergence manually and retry"
        }
    } else {
        Warn "Remote branch origin/$branch does not exist; skipping code update"
    }

    if ($stashed) {
        Info "Restoring local changes..."
        & git stash pop 2>$null
        if ($LASTEXITCODE -ne 0) { Warn "Stash restore had conflicts; changes remain in the stash (see git stash list)" }
    }

    Info "Refreshing dependencies..."
    Ensure-Installed

    if ($wasRunning) {
        Info "Restarting service to apply the new version..."
        Cmd-Restart
    } else {
        Ok "Update complete (service was not running; stays stopped)"
    }
}

# ---------- Entry point ----------
if ($args.Count -ne 1) {
    Write-Host "doc-translator service manager"
    Write-Host ""
    Write-Host "Usage: .\manage.ps1 {install|start|stop|restart|status|update}"
    Write-Host ""
    Write-Host "  install   Install or repair the virtual environment and dependencies (idempotent)."
    Write-Host "  start     Start the service in the background and wait for the health check (skips if already running)."
    Write-Host "  stop      Graceful stop (no side effects if not running)."
    Write-Host "  restart   Restart the service."
    Write-Host "  status    Show run status (exit code 3 means not running)."
    Write-Host "  update    Pull latest code, refresh deps, and restart if needed."
    exit 1
}
switch ($args[0]) {
    'install' { Cmd-Install }
    'start'   { Cmd-Start }
    'stop'    { Cmd-Stop }
    'restart' { Cmd-Restart }
    'status'  { Cmd-Status }
    'update'  { Cmd-Update }
    default   {
        Die "Unknown command `"$($args[0])`". Available: install start stop restart status update"
    }
}
