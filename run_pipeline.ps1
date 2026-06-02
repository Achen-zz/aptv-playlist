$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".runtime\python\python.exe"
$env:PYTHONDONTWRITEBYTECODE = "1"

if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

& $python (Join-Path $root "fetch_raw.py")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $python (Join-Path $root "playlist_builder.py") @args
exit $LASTEXITCODE
