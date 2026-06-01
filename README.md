# Local IPTV candidate builder

This directory contains a local-first IPTV playlist pipeline for APTV. It
collects public candidate playlists, filters them through an explicit channel
allowlist, probes playable streams with `ffprobe`, and separates public-ready
entries from experimental candidates.

## Files

- `fetch_raw.py`: downloads and merges candidate lists into `raw.m3u`.
- `channels.json`: source list and exact channel aliases.
- `playlist_builder.py`: probes candidates and builds APTV playlists.
- `prepare_publish.py`: validates and creates the GitHub Pages snapshot.
- `raw_report.json`: fetch summary.
- `health_report.json`: local probe details.
- `playlist.m3u`: formal playlist. Only approved, playable, non-sensitive URLs
  are eligible.
- `experimental.m3u`: local review list. Paid-channel candidates, IPv6-only
  entries, failed entries, and unselected alternatives stay here.
- `epg.xml`: placeholder for a later approved XMLTV merge.
- `public/`: generated HTTPS publication snapshot. Only this directory is
  uploaded by the Pages workflow.

## Run

Windows:

```bat
run_pipeline.cmd
```

Quick smoke run:

```bat
run_pipeline.cmd --limit 24 --timeout 6
```

`ffprobe` uses direct connections by default because some HLS streams do not
work correctly through desktop HTTP proxies. To test through `HTTP_PROXY` and
`HTTPS_PROXY` instead, add `--proxy-mode environment`.

Parser tests:

```powershell
.\.runtime\python\python.exe -m unittest -v
```

The current network has no usable IPv6 route. The domestic IPv6 playlist is
still retained in `raw.m3u` and marked `ipv6-unavailable` so it can be tested
again after IPv6 is enabled.

## GitHub Pages

The workflow in `.github/workflows/publish-pages.yml` runs on every push, on
manual dispatch, and every six hours. It uploads only `public/` to GitHub
Pages. After creating a public GitHub repository and enabling Pages with
GitHub Actions, use:

```text
https://<github-user>.github.io/<repository>/playlist.m3u
```

The included Windows helpers perform that setup:

```bat
login_github.cmd
publish_github.cmd
```

## Boundaries

`raw.m3u`, `experimental.m3u`, and health reports are local review artifacts.
Do not publish them. Do not add DRM bypasses, login-token extraction, cookies,
or private subscription credentials. If a later GitHub Pages job is added,
publish only `playlist.m3u` and an approved `epg.xml`.
