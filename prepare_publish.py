#!/usr/bin/env python3
"""Create a public GitHub Pages snapshot from the approved local playlist."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_raw import parse_m3u, write_text_atomic


DEFAULT_PLAYLIST = ROOT / "playlist.m3u"
DEFAULT_EXPANDED = ROOT / "playlist-expanded.m3u"
DEFAULT_IPV6 = ROOT / "playlist-ipv6.m3u"
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


def ensure_public_entry(entry, allowed_tiers: set[str] | None = None) -> None:
    allowed_tiers = allowed_tiers or {"approved-candidate"}
    tier = entry.attrs.get("x-review-tier", "")
    if tier not in allowed_tiers:
        raise ValueError(f"{entry.name}: non-approved tier cannot be published: {tier or '<missing>'}")
    if SENSITIVE_TEXT.search("\n".join([entry.url, *entry.directives, *entry.attrs.values()])):
        raise ValueError(f"{entry.name}: sensitive text cannot be published")
    try:
        query_keys = {key.lower() for key, _ in parse_qsl(urlsplit(entry.url).query)}
    except ValueError as error:
        raise ValueError(f"{entry.name}: invalid URL: {error}") from error
    if query_keys & SENSITIVE_QUERY_KEYS:
        raise ValueError(f"{entry.name}: sensitive query parameter cannot be published")


def render_index(generated_at: str, channels: list[dict[str, str]], expanded_count: int, ipv6_count: int) -> str:
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
    <p>Updated: <code>{html.escape(generated_at)}</code></p>
    <ul>
      <li><a href="playlist.m3u">playlist.m3u</a>: strict, currently healthy approved streams.</li>
      <li><a href="playlist-expanded.m3u">playlist-expanded.m3u</a>: {expanded_count} non-premium IPv4 candidates for manual selection.</li>
      <li><a href="playlist-ipv6.m3u">playlist-ipv6.m3u</a>: {ipv6_count} domestic IPv6 candidates for IPv6-capable networks.</li>
      <li><a href="epg.xml">epg.xml</a> | <a href="status.json">status.json</a></li>
    </ul>
    <table>
      <thead><tr><th>Channel</th><th>Height</th><th>Category</th></tr></thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </body>
</html>
"""


def validated_playlist(path: Path, allowed_tiers: set[str], allow_empty: bool = False) -> tuple[str, list]:
    text = path.read_text(encoding="utf-8-sig")
    entries = parse_m3u(text)
    if not entries and not allow_empty:
        raise ValueError(f"Refusing to publish an empty playlist: {path.name}")
    for entry in entries:
        ensure_public_entry(entry, allowed_tiers)
    return text, entries


def build_snapshot(
    playlist: Path,
    epg: Path,
    output: Path,
    expanded: Path | None = None,
    ipv6: Path | None = None,
) -> dict:
    playlist_text = playlist.read_text(encoding="utf-8-sig")
    playlist_text, entries = validated_playlist(playlist, {"approved-candidate"})
    expanded_text, expanded_entries = validated_playlist(
        expanded or playlist,
        {"approved-candidate", "review-required"},
    )
    ipv6_text, ipv6_entries = validated_playlist(
        ipv6 or playlist,
        {"approved-candidate", "ipv6-unavailable"},
        allow_empty=True,
    )

    resolved_output = output.resolve()
    if resolved_output == ROOT.resolve() or ROOT.resolve() not in resolved_output.parents:
        raise ValueError("Output directory must stay inside the project directory")
    output.mkdir(parents=True, exist_ok=True)

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
    status = {
        "generated_at": generated_at,
        "channel_count": len(channels),
        "expanded_channel_count": len(expanded_entries),
        "ipv6_channel_count": len(ipv6_entries),
        "channels": channels,
    }
    write_text_atomic(output / "playlist.m3u", playlist_text)
    write_text_atomic(output / "playlist-expanded.m3u", expanded_text)
    write_text_atomic(output / "playlist-ipv6.m3u", ipv6_text)
    write_text_atomic(output / "epg.xml", epg.read_text(encoding="utf-8-sig"))
    write_text_atomic(
        output / "status.json",
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
    )
    write_text_atomic(
        output / "index.html",
        render_index(generated_at, channels, len(expanded_entries), len(ipv6_entries)),
    )
    write_text_atomic(output / ".nojekyll", "")
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", type=Path, default=DEFAULT_PLAYLIST)
    parser.add_argument("--expanded", type=Path, default=DEFAULT_EXPANDED)
    parser.add_argument("--ipv6", type=Path, default=DEFAULT_IPV6)
    parser.add_argument("--epg", type=Path, default=DEFAULT_EPG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    status = build_snapshot(args.playlist, args.epg, args.output, args.expanded, args.ipv6)
    print(
        f"Wrote public snapshot with {status['channel_count']} strict, "
        f"{status['expanded_channel_count']} expanded, and "
        f"{status['ipv6_channel_count']} IPv6 channels to {args.output}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
