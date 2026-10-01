$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$trackerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $trackerPython)) { throw 'Local Python environment is missing.' }
& $trackerPython (Join-Path $PSScriptRoot 'tracker.py')
exit $LASTEXITCODE
