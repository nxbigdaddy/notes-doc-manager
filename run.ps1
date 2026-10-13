$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project environment is missing. Follow README.md to install dependencies.'
}
Start-Process -FilePath $python -ArgumentList @('-m', 'notes') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
