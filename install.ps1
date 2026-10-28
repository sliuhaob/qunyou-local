$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $PSScriptRoot
try {
    $taskPython = $null
    foreach ($version in @('-3.12', '-3.11', '-3.13')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            try {
                $candidate = & py $version -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $candidate) { $taskPython = $candidate.Trim(); break }
            } catch { }
        }
    }
    if (-not $taskPython -and (Get-Command python -ErrorAction SilentlyContinue)) {
        try {
            $candidate = & python -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,13); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) { $taskPython = $candidate.Trim() }
        } catch { }
    }
    if (-not $taskPython) { throw 'Install Python 3.12 (64-bit, include Tcl/Tk) from https://www.python.org/downloads/windows/ then run this again.' }
    & $taskPython -c "import struct, tkinter; assert struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) { throw '64-bit Python with Tkinter is required.' }
    & $taskPython -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create local Python environment.' }
    $taskVenv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    & $taskVenv -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network and retry.' }
    $taskHasCodex = $false
    try {
        & $taskVenv -c 'from local_codex import resolve_codex_executable; print(resolve_codex_executable())' 2>$null
        $taskHasCodex = $LASTEXITCODE -eq 0
    } catch { }
    if (-not $taskHasCodex) {
        $taskNpm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not $taskNpm) { throw 'Install Node.js LTS from https://nodejs.org/ then run this again (used to install the official Codex CLI).' }
        & $taskNpm.Source install --prefix (Join-Path $PSScriptRoot 'tools\codex') '@openai/codex' --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Codex CLI installation failed. Check network and retry.' }
    }
    Write-Host 'Installation complete. Open 02-open.cmd, then sign in with your own GPT account.' -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
