# doc-translator one-click installer (Windows PowerShell)
#
# One-line install (PowerShell 5.1+):
#   powershell -c "irm https://raw.githubusercontent.com/ilysom0611/doc-translator/main/install.ps1 | iex"
#
# Optional environment variables:
#   DT_DIR     Install directory (default: ~\doc-translator)
#   DT_BRANCH  Branch (default: main)

$ErrorActionPreference = 'Stop'

$RepoUrl  = if ($env:DT_REPO)   { $env:DT_REPO }   else { 'https://github.com/ilysom0611/doc-translator.git' }
$Branch   = if ($env:DT_BRANCH) { $env:DT_BRANCH } else { 'main' }
$Dest     = if ($env:DT_DIR)    { $env:DT_DIR }    else { Join-Path $HOME 'doc-translator' }

function Fail($msg) { Write-Host "[install] error: $msg" -ForegroundColor Red; exit 1 }
function Log($msg)  { Write-Host "[install] $msg" -ForegroundColor Green }

# ---------- Pre-flight checks ----------
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { Fail 'git not found. Please install it: https://git-scm.com/download/win' }

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    # A Microsoft Store alias may exist but not actually work — try the py launcher as a fallback.
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $script:PyExe = 'py' } else { Fail 'Python not found. Install 3.10+ and check Add to PATH: https://www.python.org/downloads/' }
} else { $script:PyExe = 'python' }

& $script:PyExe -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { Fail "Python version must be >= 3.10" }

# ---------- Clone / update source ----------
if (Test-Path (Join-Path $Dest '.git')) {
    Log "Destination exists; pulling latest code: $Dest"
    git -C $Dest fetch --depth 1 origin $Branch
    git -C $Dest reset --hard "origin/$Branch"
} else {
    Log "Cloning repository to $Dest"
    git clone --depth 1 --branch $Branch $RepoUrl $Dest
    if ($LASTEXITCODE -ne 0) { Fail 'Clone failed; please check your network and the repository URL' }
}
Set-Location $Dest

# ---------- Virtual environment + dependencies ----------
Log 'Creating virtual environment and installing dependencies (versions are pinned)'
& $script:PyExe -m venv .venv
$vpip = Join-Path $Dest '.venv\Scripts\python.exe'
& $vpip -m pip install --upgrade pip -q
& $vpip -m pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) { Fail 'Dependency install failed; please check your network' }

$soffice = @("${env:ProgramFiles}\LibreOffice\program\soffice.exe",
             "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe") |
           Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $soffice) {
    Log 'Note: LibreOffice not detected. High-fidelity rendering will fall back to browser-side preview.'
    Log '      Optional install: https://www.libreoffice.org/'
}

Write-Host ''
Write-Host "✅ Install complete: $Dest" -ForegroundColor Green
Write-Host ''
Write-Host 'How to start:'
Write-Host "  cd $Dest"
Write-Host '  .\manage.bat start      # run in the background (use .\manage.bat stop to stop)'
Write-Host '  or double-click start.bat   # foreground window'
Write-Host ''
Write-Host 'Then open http://127.0.0.1:8765 in your browser. On first visit you will be guided to set the admin password.'
