#!/usr/bin/env python3
"""Download, filter, classify, and merge public IPTV candidate playlists."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "channels.json"
DEFAULT_OUTPUT = ROOT / "raw.m3u"
DEFAULT_REPORT = ROOT / "raw_report.json"
ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)=(?:"([^"]*)"|([^\s]+))')
CUSTOM_ATTRIBUTE_ORDER = (
    "x-source",
    "x-channel-id",
    "x-review-tier",
    "x-original-tier",
    "x-license-scope",
    "x-category",
)


@dataclass
class PlaylistEntry:
    duration: str
    attrs: dict[str, str]
    name: str
    url: str
    directives: list[str] = field(default_factory=list)
    source_id: str = ""
    source_priority: int = 999

    def clone(self) -> "PlaylistEntry":
        return copy.deepcopy(self)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_json(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def split_unquoted_comma(value: str) -> tuple[str, str]:
    quoted = False
    for index, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return value[:index], value[index + 1 :]
    return value, ""


def parse_extinf(line: str) -> tuple[str, dict[str, str], str]:
    body = line[len("#EXTINF:") :] if line.startswith("#EXTINF:") else line
    metadata, name = split_unquoted_comma(body)
    first_space = metadata.find(" ")
    duration = metadata if first_space == -1 else metadata[:first_space]
    attrs: dict[str, str] = {}
    for match in ATTRIBUTE_RE.finditer(metadata):
        attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return duration or "-1", attrs, name.strip()


def parse_m3u(text: str, source_id: str = "", source_priority: int = 999) -> list[PlaylistEntry]:
    lines = text.lstrip("\ufeff").splitlines()
    entries: list[PlaylistEntry] = []
    pending: PlaylistEntry | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            if pending and pending.url:
                entries.append(pending)
            duration, attrs, name = parse_extinf(line)
            pending = PlaylistEntry(duration, attrs, name, "", [], source_id, source_priority)
            continue
        if pending is None:
            continue
        if line.startswith("#"):
            pending.directives.append(line)
            continue
        pending.url = line
        entries.append(pending)
        pending = None
    if pending and pending.url:
        entries.append(pending)
    return entries


def quote_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_entry(entry: PlaylistEntry, extra_attrs: dict[str, str] | None = None) -> list[str]:
    attrs = dict(entry.attrs)
    if extra_attrs:
        attrs.update({key: str(value) for key, value in extra_attrs.items() if value is not None})
    ordered_keys = [key for key in attrs if key not in CUSTOM_ATTRIBUTE_ORDER]
    ordered_keys += [key for key in CUSTOM_ATTRIBUTE_ORDER if key in attrs]
    rendered_attrs = "".join(f' {key}="{quote_attr(attrs[key])}"' for key in ordered_keys)
    return [f"#EXTINF:{entry.duration}{rendered_attrs},{entry.name}", *entry.directives, entry.url]


def write_m3u(path: Path, entries: Iterable[PlaylistEntry], header: str = "#EXTM3U") -> None:
    lines = [header]
    for entry in entries:
        lines.extend(render_entry(entry))
    write_text_atomic(path, "\n".join(lines) + "\n")


def has_ipv6_literal(url: str) -> bool:
    try:
        return ":" in (urlsplit(url).hostname or "")
    except ValueError:
        return False


def has_ipv6_route() -> bool:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.connect(("2606:4700:4700::1111", 53))
            return not sock.getsockname()[0].startswith("fe80:")
        finally:
            sock.close()
    except OSError:
        return False


def target_for_entry(entry: PlaylistEntry, targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    values = [
        entry.name,
        entry.attrs.get("tvg-name", ""),
        entry.attrs.get("tvg-id", ""),
    ]
    for value in values:
        if not value:
            continue
        for target in targets:
            for alias in target["aliases"]:
                if re.compile(alias, re.IGNORECASE).search(value):
                    return target
    return None


def classify_tier(
    entry: PlaylistEntry,
    target: dict[str, Any],
    source: dict[str, Any],
    ipv6_available: bool,
) -> tuple[str, str]:
    original_tier = source["default_tier"]
    if target.get("premium", False):
        return "experimental-premium", original_tier
    if has_ipv6_literal(entry.url) and not ipv6_available:
        return "ipv6-unavailable", original_tier
    if any(entry.url.startswith(prefix) for prefix in source.get("approved_url_prefixes", [])):
        return "approved-candidate", original_tier
    return original_tier, original_tier


def enrich_entry(
    entry: PlaylistEntry,
    target: dict[str, Any],
    source: dict[str, Any],
    ipv6_available: bool,
) -> PlaylistEntry:
    enriched = entry.clone()
    tier, original_tier = classify_tier(enriched, target, source, ipv6_available)
    enriched.attrs.update(
        {
            "x-source": source["id"],
            "x-channel-id": target["id"],
            "x-review-tier": tier,
            "x-original-tier": original_tier,
            "x-license-scope": source["license_scope"],
            "x-category": target["category"],
        }
    )
    return enriched


def normalized_url(url: str) -> str:
    return url.strip()


def deduplicate(entries: Iterable[PlaylistEntry]) -> tuple[list[PlaylistEntry], int]:
    unique: dict[str, PlaylistEntry] = {}
    duplicate_count = 0
    for entry in sorted(entries, key=lambda item: item.source_priority):
        key = normalized_url(entry.url)
        if key in unique:
            duplicate_count += 1
            continue
        unique[key] = entry
    return list(unique.values()), duplicate_count


def fetch_text(url: str, timeout: float) -> tuple[str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, text/plain, */*",
            "User-Agent": "iptv-candidate-builder/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace"), len(body)


def sort_entries(entries: Iterable[PlaylistEntry]) -> list[PlaylistEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            entry.attrs.get("x-category", ""),
            entry.attrs.get("x-channel-id", ""),
            entry.source_priority,
            entry.name,
            entry.url,
        ),
    )


def build_candidates(config: dict[str, Any], timeout: float, retries: int = 3) -> tuple[list[PlaylistEntry], dict[str, Any]]:
    ipv6_available = has_ipv6_route()
    report: dict[str, Any] = {
        "generated_at": utc_now(),
        "ipv6_route_available": ipv6_available,
        "sources": [],
    }
    all_entries: list[PlaylistEntry] = []
    targets = config["targets"]
    for source in config["sources"]:
        source_report: dict[str, Any] = {
            "id": source["id"],
            "url": source["url"],
            "status": "pending",
            "downloaded_bytes": 0,
            "parsed_entries": 0,
            "matched_entries": 0,
        }
        try:
            last_error: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    text, downloaded_bytes = fetch_text(source["url"], timeout)
                    source_report["attempts"] = attempt
                    break
                except (OSError, UnicodeError, urllib.error.URLError) as error:
                    last_error = error
                    if attempt < retries:
                        time.sleep(attempt)
            else:
                raise last_error or OSError("download failed")
            parsed = parse_m3u(text, source["id"], source["priority"])
            matched: list[PlaylistEntry] = []
            for entry in parsed:
                target = target_for_entry(entry, targets)
                if target is not None:
                    matched.append(enrich_entry(entry, target, source, ipv6_available))
            source_report.update(
                {
                    "status": "ok",
                    "downloaded_bytes": downloaded_bytes,
                    "parsed_entries": len(parsed),
                    "matched_entries": len(matched),
                }
            )
            all_entries.extend(matched)
        except (OSError, UnicodeError, urllib.error.URLError) as error:
            source_report.update({"status": "error", "error": str(error)})
        report["sources"].append(source_report)
    unique, duplicate_count = deduplicate(all_entries)
    sorted_entries = sort_entries(unique)
    report["summary"] = {
        "matched_before_deduplication": len(all_entries),
        "duplicates_removed": duplicate_count,
        "output_entries": len(sorted_entries),
        "tiers": dict(Counter(entry.attrs["x-review-tier"] for entry in sorted_entries)),
        "channels": dict(Counter(entry.attrs["x-channel-id"] for entry in sorted_entries)),
    }
    return sorted_entries, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_json(args.config)
    entries, report = build_candidates(config, args.timeout, args.retries)
    write_m3u(args.output, entries)
    write_json(args.report, report)
    print(
        f"Wrote {len(entries)} candidates to {args.output.name}; "
        f"IPv6 route available: {report['ipv6_route_available']}"
    )
    return 0 if entries else 1


if __name__ == "__main__":
    sys.exit(main())
