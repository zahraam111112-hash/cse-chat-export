# Start ChatGPT Share Exporter as a local desktop app.
#
# Works in two layouts:
#   * the unpacked release  — python\ + app\ + windows\ side by side
#   * a developer checkout  — .venv\ next to app\
# Ctrl+C in this window stops the app.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root       = Split-Path -Parent $Here
$PythonExe  = $null

$Bundled = Join-Path $Root "python\python.exe"
$Venv    = Join-Path $Root ".venv\Scripts\python.exe"

if (Test-Path $Bundled) {
    $PythonExe = $Bundled
} elseif (Test-Path $Venv) {
    # A bundled runtime carries its own site-packages, so --isolated would hide
    # them; only the system interpreter is run with -I.
    $PythonExe = $Venv
} else {
    foreach ($Candidate in @("py", "python")) {
        $Cmd = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Cmd) {
            & $Cmd.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                if ($Candidate -eq "py") { $PythonExe = $Cmd.Source; $LauncherArgs = @("-3") }
                else                     { $PythonExe = $Cmd.Source; $LauncherArgs = @() }
                break
            }
        }
    }
}

if (-not $PythonExe) {
    Write-Host "Python 3.10+ was not found." -ForegroundColor Red
    Write-Host "Use the installer from the Releases page, or install Python from python.org."
    Read-Host "Press Enter to close"
    exit 1
}

# The bundled runtime behaves exactly like an installed one; the system one is
# run isolated so a stray module in the working directory cannot shadow ours.
if ($PythonExe -eq $Bundled) { $Isolated = @() } else { $Isolated = @("-I") }
$AllArgs = @() + $Isolated + @((Join-Path $Here "desktop.py"))

Write-Host "Starting ChatGPT Share Exporter..." -ForegroundColor Cyan
Write-Host "The first launch can take a few seconds while Chromium is prepared."
Write-Host ""

try {
    & $PythonExe @AllArgs
    $Code = $LASTEXITCODE
} catch {
    Write-Host "The app stopped: $_" -ForegroundColor Red
    $Code = 1
}

if ($Code -ne 0 -and $Code -ne 130) {
    Write-Host ""
    Write-Host "The app exited with code $Code." -ForegroundColor Red
    Read-Host "Press Enter to close"
}
exit $Code
