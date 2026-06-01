param(
    [string]$Repository = "aptv-playlist"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$gh = Get-ChildItem (Join-Path $root ".runtime\tools\gh") -Recurse -File -Filter gh.exe |
    Select-Object -First 1 -ExpandProperty FullName
$git = Join-Path $root ".runtime\tools\mingit\cmd\git.exe"

if (-not $gh -or -not (Test-Path $gh)) {
    throw "Portable GitHub CLI was not found under .runtime\tools\gh."
}
if (-not (Test-Path $git)) {
    throw "Portable Git was not found under .runtime\tools\mingit."
}

$env:PATH = "$(Split-Path -Parent $git);$(Split-Path -Parent $gh);$env:PATH"

function Invoke-GhWithRetry {
    param(
        [scriptblock]$Command,
        [int]$Attempts = 4
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $output = & $Command 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $output
        }
        if ($attempt -lt $Attempts) {
            Start-Sleep -Seconds $attempt
        }
    }
    throw "GitHub CLI command failed after $Attempts attempts."
}

& $gh auth status --hostname github.com *> $null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated. Run login_github.cmd first."
}

$owner = ([string](Invoke-GhWithRetry { & $gh api user --jq .login })).Trim()
if (-not $owner) {
    throw "Could not determine the authenticated GitHub user."
}

Push-Location $root
try {
    if (-not (Test-Path ".git")) {
        & $git init --initial-branch=main
        if ($LASTEXITCODE -ne 0) { throw "git init failed." }
    }

    & $git config user.name $owner
    & $git config user.email "$owner@users.noreply.github.com"
    & $git add --all
    & $git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        & $git commit -m "Add automated APTV playlist publisher"
        if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
    }

    $origin = & $git remote 2>$null | Where-Object { $_ -eq "origin" }
    if (-not $origin) {
        $remoteName = & cmd.exe /d /c "`"$gh`" api `"repos/$owner/$Repository`" --jq .name 2>nul"
        if ($LASTEXITCODE -eq 0 -and $remoteName) {
            & $git remote add origin "https://github.com/$owner/$Repository.git"
        } else {
            & $gh repo create "$owner/$Repository" --public --source . --remote origin
            if ($LASTEXITCODE -ne 0) { throw "GitHub repository creation failed." }
        }
    }

    & $git push --set-upstream origin main
    if ($LASTEXITCODE -ne 0) { throw "git push failed." }

    $pages = & cmd.exe /d /c "`"$gh`" api `"repos/$owner/$Repository/pages`" 2>nul"
    if ($LASTEXITCODE -eq 0 -and $pages) {
        Invoke-GhWithRetry { & $gh api --method PUT "repos/$owner/$Repository/pages" -f build_type=workflow } *> $null
    } else {
        Invoke-GhWithRetry { & $gh api --method POST "repos/$owner/$Repository/pages" -f build_type=workflow } *> $null
    }

    $url = "https://$owner.github.io/$Repository/playlist.m3u"
    Set-Content -Path (Join-Path $root "published_url.txt") -Value $url -Encoding UTF8
    Write-Output "Repository: https://github.com/$owner/$Repository"
    Write-Output "APTV URL: $url"
    Write-Output "The push-triggered GitHub Actions deployment is now starting."
}
finally {
    Pop-Location
}
