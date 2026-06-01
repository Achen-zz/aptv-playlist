#!/usr/bin/env python3
"""Create a public GitHub Pages snapshot from the approved local playlist."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_raw import parse_m3u


DEFAULT_PLAYLIST = ROOT / "playlist.m3u"
DEFAULT_EPG = ROOT / "epg.xml"
DEFAULT_OUTPUT = ROOT / "public"
SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization|cookie|access_token|bearer\s+|jwt|signature|[?&](?:auth|key|token)=)"
)
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "jwt",
    "key",
    "signature",
    "token",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def ensure_public_entry(entry) -> None:
    tier = entry.attrs.get("x-review-tier", "")
    if tier != "approved-candidate":
        raise ValueError(f"{entry.name}: non-approved tier cannot be published: {tier or '<missing>'}")
    if SENSITIVE_TEXT.search("\n".join([entry.url, *entry.directives, *entry.attrs.values()])):
        raise ValueError(f"{entry.name}: sensitive text cannot be published")
    try:
        query_keys = {key.lower() for key, _ in parse_qsl(urlsplit(entry.url).query)}
    except ValueError as error:
        raise ValueError(f"{entry.name}: invalid URL: {error}") from error
    if query_keys & SENSITIVE_QUERY_KEYS:
        raise ValueError(f"{entry.name}: sensitive query parameter cannot be published")


def render_index(generated_at: str, channels: list[dict[str, str]]) -> str:
    rows = "\n".join(
        "      <tr>"
        f"<td>{html.escape(channel['name'])}</td>"
        f"<td>{html.escape(channel['height'])}</td>"
        f"<td>{html.escape(channel['category'])}</td>"
        "</tr>"
        for channel in channels
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>APTV Playlist</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 54rem; padding: 0 1rem; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid #ddd; padding: .6rem; text-align: left; }}
      code {{ background: #f4f4f4; padding: .15rem .3rem; }}
    </style>
  </head>
  <body>
    <h1>APTV Playlist</h1>
    <p>Approved public streams only. Updated: <code>{html.escape(generated_at)}</code></p>
    <p><a href="playlist.m3u">playlist.m3u</a> | <a href="epg.xml">epg.xml</a> | <a href="status.json">status.json</a></p>
    <table>
      <thead><tr><th>Channel</th><th>Height</th><th>Category</th></tr></thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </body>
</html>
"""


def build_snapshot(playlist: Path, epg: Path, output: Path) -> dict:
    playlist_text = playlist.read_text(encoding="utf-8-sig")
    entries = parse_m3u(playlist_text)
    if not entries:
        raise ValueError("Refusing to publish an empty playlist")
    for entry in entries:
        ensure_public_entry(entry)

    resolved_output = output.resolve()
    if resolved_output == ROOT.resolve() or ROOT.resolve() not in resolved_output.parents:
        raise ValueError("Output directory must stay inside the project directory")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    generated_at = utc_now()
    channels = [
        {
            "id": entry.attrs.get("x-channel-id", entry.name),
            "name": entry.name,
            "height": entry.attrs.get("x-height", ""),
            "category": entry.attrs.get("x-category", ""),
        }
        for entry in entries
    ]
    status = {"generated_at": generated_at, "channel_count": len(channels), "channels": channels}
    (output / "playlist.m3u").write_text(playlist_text, encoding="utf-8", newline="\n")
    shutil.copyfile(epg, output / "epg.xml")
    (output / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "index.html").write_text(render_index(generated_at, channels), encoding="utf-8", newline="\n")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", type=Path, default=DEFAULT_PLAYLIST)
    parser.add_argument("--epg", type=Path, default=DEFAULT_EPG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    status = build_snapshot(args.playlist, args.epg, args.output)
    print(f"Wrote public snapshot with {status['channel_count']} channels to {args.output}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
