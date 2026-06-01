$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$gh = Get-ChildItem (Join-Path $root ".runtime\tools\gh") -Recurse -File -Filter gh.exe |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $gh -or -not (Test-Path $gh)) {
    throw "Portable GitHub CLI was not found under .runtime\tools\gh."
}

& $gh auth login --hostname github.com --git-protocol https --web --clipboard
exit $LASTEXITCODE
