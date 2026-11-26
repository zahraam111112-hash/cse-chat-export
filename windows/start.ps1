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

# The release bundles an embeddable interpreter at python\python.exe; a venv
# keeps its own under Scripts\.
$Bundled = Join-Path $Root "python\python.exe"
$Venv    = Join-Path $Root ".venv\Scripts\python.exe"

if (Test-Path $Bundled) {
    $PythonExe = $Bundled
} elseif (Test-Path $Venv) {
    $PythonExe = $Venv
} else {
    $LauncherArgs = @()
    foreach ($Candidate in @("py", "python")) {
        $Cmd = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Cmd) {
            & $Cmd.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $PythonExe = $Cmd.Source
                if ($Candidate -eq "py") { $LauncherArgs = @("-3") }
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

# A bundled interpreter carries its own site-packages, so --isolated would hide
# them. The system one is run isolated so a stray module in the working
# directory cannot shadow ours.
if ($PythonExe -eq $Bundled -or $PythonExe -eq $Venv) { $Isolated = @() } else { $Isolated = @("-I") }
$AllArgs = @() + $LauncherArgs + $Isolated + @((Join-Path $Here "desktop.py"))

Write-Host "Starting ChatGPT Share Exporter..." -ForegroundColor Cyan
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
