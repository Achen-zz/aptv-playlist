$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".runtime\python\python.exe"

if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

& $python (Join-Path $root "fetch_raw.py")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $python (Join-Path $root "playlist_builder.py") @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $python (Join-Path $root "prepare_publish.py")
exit $LASTEXITCODE
