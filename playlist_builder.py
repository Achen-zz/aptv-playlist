#!/usr/bin/env python3
"""Probe local IPTV candidates and build formal and experimental playlists."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_raw import PlaylistEntry, parse_m3u, render_entry, utc_now, write_json


DEFAULT_RAW = ROOT / "raw.m3u"
DEFAULT_FORMAL = ROOT / "playlist.m3u"
DEFAULT_EXPERIMENTAL = ROOT / "experimental.m3u"
DEFAULT_REPORT = ROOT / "health_report.json"
DEFAULT_STATE = ROOT / "health_state.json"
DEFAULT_EPG = ROOT / "epg.xml"
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "jwt",
    "key",
    "signature",
    "token",
}


def locate_ffprobe(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    from_path = shutil.which("ffprobe")
    if from_path:
        return from_path
    matches = sorted((ROOT / ".runtime" / "ffmpeg").glob("**/ffprobe.exe"))
    if matches:
        return str(matches[0])
    raise FileNotFoundError("ffprobe was not found. Install FFmpeg or place it under .runtime/ffmpeg.")


def entry_key(entry: PlaylistEntry) -> str:
    return hashlib.sha256(entry.url.encode("utf-8")).hexdigest()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def header_options(entry: PlaylistEntry) -> list[str]:
    options: list[str] = []
    user_agent = entry.attrs.get("http-user-agent")
    referer = entry.attrs.get("http-referrer") or entry.attrs.get("http-referer")
    extra_headers: list[str] = []
    if user_agent:
        options.extend(["-user_agent", user_agent])
    if referer:
        options.extend(["-referer", referer])
    header_value = entry.attrs.get("http-header")
    if header_value:
        key, separator, value = header_value.partition("=")
        if separator:
            extra_headers.append(f"{key}: {value}")
    for directive in entry.directives:
        if directive.startswith("#EXTVLCOPT:http-user-agent=") and not user_agent:
            options.extend(["-user_agent", directive.split("=", 1)[1]])
        elif directive.startswith("#EXTVLCOPT:http-referrer=") and not referer:
            options.extend(["-referer", directive.split("=", 1)[1]])
        elif directive.startswith("#EXTVLCOPT:http-referer=") and not referer:
            options.extend(["-referer", directive.split("=", 1)[1]])
    if extra_headers:
        options.extend(["-headers", "\r\n".join(extra_headers) + "\r\n"])
    return options


def request_headers(entry: PlaylistEntry) -> dict[str, str]:
    headers = {"User-Agent": entry.attrs.get("http-user-agent", "iptv-candidate-builder/1.0")}
    referer = entry.attrs.get("http-referrer") or entry.attrs.get("http-referer")
    header_value = entry.attrs.get("http-header")
    if referer:
        headers["Referer"] = referer
    if header_value:
        key, separator, value = header_value.partition("=")
        if separator:
            headers[key] = value
    for directive in entry.directives:
        if directive.startswith("#EXTVLCOPT:http-user-agent="):
            headers["User-Agent"] = directive.split("=", 1)[1]
        elif directive.startswith(("#EXTVLCOPT:http-referrer=", "#EXTVLCOPT:http-referer=")):
            headers["Referer"] = directive.split("=", 1)[1]
    return headers


def http_preflight(entry: PlaylistEntry, timeout: float, proxy_mode: str) -> dict[str, Any]:
    try:
        parts = urlsplit(entry.url)
    except ValueError as error:
        return {"status": "error", "error": str(error)}
    if parts.scheme not in {"http", "https"}:
        return {"status": "skipped", "reason": f"Unsupported URL scheme: {parts.scheme or 'missing'}"}
    request = urllib.request.Request(entry.url, headers={**request_headers(entry), "Range": "bytes=0-65535"})
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if proxy_mode == "direct"
        else urllib.request.build_opener()
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=min(timeout, 8.0)) as response:
            response.read(65_536)
            return {
                "status": int(response.status),
                "final_url": response.geturl(),
                "content_type": response.headers.get_content_type(),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": int(error.code),
            "final_url": error.geturl(),
            "error": str(error.reason),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except (OSError, urllib.error.URLError) as error:
        return {
            "status": "error",
            "error": str(error),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }


def has_sensitive_credentials(entry: PlaylistEntry) -> bool:
    try:
        query_keys = {key.lower() for key, _ in parse_qsl(urlsplit(entry.url).query)}
    except ValueError:
        return True
    if query_keys & SENSITIVE_QUERY_KEYS:
        return True
    protected_text = " ".join(entry.directives + [entry.attrs.get("http-header", "")]).lower()
    return "authorization" in protected_text or "cookie" in protected_text


def ffprobe_environment(proxy_mode: str) -> dict[str, str]:
    environment = os.environ.copy()
    if proxy_mode == "direct":
        for key in (
            "ALL_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "all_proxy",
            "https_proxy",
            "http_proxy",
        ):
            environment.pop(key, None)
    return environment


def probe_entry(entry: PlaylistEntry, ffprobe: str, timeout: float, proxy_mode: str) -> dict[str, Any]:
    started = time.monotonic()
    preflight = http_preflight(entry, timeout, proxy_mode)
    command = [
        ffprobe,
        "-v",
        "error",
        "-rw_timeout",
        str(int(timeout * 1_000_000)),
        "-analyzeduration",
        "3000000",
        "-probesize",
        "3000000",
        *header_options(entry),
        "-read_intervals",
        "%+#1",
        "-show_entries",
        "format=bit_rate,format_name:stream=index,codec_type,codec_name,width,height,bit_rate",
        "-of",
        "json",
        entry.url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 15,
            check=False,
            env=ffprobe_environment(proxy_mode),
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip().splitlines()
            return {
                "status": "failed",
                "elapsed_ms": elapsed_ms,
                "http": preflight,
                "error": message[-1][:500] if message else f"ffprobe exited {completed.returncode}",
            }
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams", [])
        if not streams:
            return {"status": "failed", "elapsed_ms": elapsed_ms, "http": preflight, "error": "ffprobe found no streams"}
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
        width = max((int(stream.get("width") or 0) for stream in videos), default=0)
        height = max((int(stream.get("height") or 0) for stream in videos), default=0)
        bitrates = [
            int(value)
            for value in [payload.get("format", {}).get("bit_rate"), *(s.get("bit_rate") for s in streams)]
            if value and str(value).isdigit()
        ]
        return {
            "status": "success",
            "elapsed_ms": elapsed_ms,
            "http": preflight,
            "segment_validation": "ffprobe-read",
            "width": width,
            "height": height,
            "bit_rate": max(bitrates, default=0),
            "video_codecs": sorted({stream.get("codec_name", "") for stream in videos if stream.get("codec_name")}),
            "audio_codecs": sorted({stream.get("codec_name", "") for stream in audios if stream.get("codec_name")}),
            "format_name": payload.get("format", {}).get("format_name", ""),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "http": preflight,
            "error": f"ffprobe exceeded {timeout + 15:.1f}s timeout",
        }
    except (json.JSONDecodeError, OSError) as error:
        return {
            "status": "failed",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "http": preflight,
            "error": str(error),
        }


def score_probe(probe: dict[str, Any]) -> int:
    height = int(probe.get("height", 0))
    bit_rate = int(probe.get("bit_rate", 0))
    elapsed_ms = int(probe.get("elapsed_ms", 0))
    quality = 4_000_000 if height >= 2160 else 3_000_000 if height >= 1080 else 2_000_000 if height >= 720 else 1_000_000
    return quality + min(bit_rate // 1000, 500_000) - min(elapsed_ms, 60_000)


def write_playlist(path: Path, entries: list[tuple[PlaylistEntry, dict[str, str]]]) -> None:
    lines = ["#EXTM3U"]
    for entry, extra_attrs in entries:
        lines.extend(render_entry(entry, extra_attrs))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def ensure_epg_placeholder(path: Path) -> None:
    if path.exists():
        return
    root = ElementTree.Element("tv", {"generator-info-name": "iptv-candidate-builder"})
    root.append(ElementTree.Comment(" Placeholder. Merge approved XMLTV sources here in a later phase. "))
    ElementTree.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def update_state(
    state: dict[str, Any],
    entry: PlaylistEntry,
    probe: dict[str, Any],
    checked_at: str,
) -> dict[str, Any]:
    key = entry_key(entry)
    previous = state.get(key, {})
    if probe["status"] == "success":
        current = {
            "url": entry.url,
            "channel_id": entry.attrs.get("x-channel-id", ""),
            "consecutive_failures": 0,
            "last_checked": checked_at,
            "last_success": checked_at,
            "last_success_probe": probe,
        }
    else:
        current = {
            **previous,
            "url": entry.url,
            "channel_id": entry.attrs.get("x-channel-id", ""),
            "consecutive_failures": int(previous.get("consecutive_failures", 0)) + 1,
            "last_checked": checked_at,
            "last_error": probe.get("error", "unknown error"),
        }
    state[key] = current
    return current


def publication_probe(probe: dict[str, Any], state_record: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    if probe["status"] == "success":
        return "healthy", probe
    if state_record.get("last_success_probe") and int(state_record.get("consecutive_failures", 0)) < 3:
        return "stale", state_record["last_success_probe"]
    return "excluded", None


def build(args: argparse.Namespace) -> dict[str, Any]:
    ffprobe = locate_ffprobe(args.ffprobe)
    raw_entries = parse_m3u(args.raw.read_text(encoding="utf-8-sig"))
    state: dict[str, Any] = read_json(args.state, {})
    checked_at = utc_now()
    eligible = [
        entry
        for entry in raw_entries
        if entry.attrs.get("x-review-tier") != "ipv6-unavailable"
    ]
    selected_for_probe = eligible[: args.limit] if args.limit else eligible
    selected_keys = {entry_key(entry) for entry in selected_for_probe}
    probes: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_entry = {
            executor.submit(probe_entry, entry, ffprobe, args.timeout, args.proxy_mode): entry
            for entry in selected_for_probe
        }
        for future in concurrent.futures.as_completed(future_to_entry):
            entry = future_to_entry[future]
            probes[entry_key(entry)] = future.result()

    report_entries: list[dict[str, Any]] = []
    publishable_by_channel: dict[str, list[tuple[PlaylistEntry, dict[str, Any], str]]] = defaultdict(list)
    experimental: list[tuple[PlaylistEntry, dict[str, str]]] = []
    for entry in raw_entries:
        key = entry_key(entry)
        tier = entry.attrs.get("x-review-tier", "review-required")
        if tier == "ipv6-unavailable":
            probe = {"status": "skipped-ipv6", "error": "No local IPv6 route was available during fetch."}
            state_record = state.get(key, {})
        elif key not in selected_keys:
            probe = {"status": "skipped-limit", "error": "Skipped by --limit for this run."}
            state_record = state.get(key, {})
        else:
            probe = probes[key]
            state_record = update_state(state, entry, probe, checked_at)
        publish_status, effective_probe = publication_probe(probe, state_record)
        credentials_blocked = has_sensitive_credentials(entry)
        if (
            tier == "approved-candidate"
            and effective_probe is not None
            and not credentials_blocked
        ):
            publishable_by_channel[entry.attrs.get("x-channel-id", entry.name)].append(
                (entry, effective_probe, publish_status)
            )
        report_entries.append(
            {
                "channel_id": entry.attrs.get("x-channel-id", ""),
                "name": entry.name,
                "source": entry.attrs.get("x-source", ""),
                "tier": tier,
                "url": entry.url,
                "probe": probe,
                "consecutive_failures": state_record.get("consecutive_failures", 0),
                "credentials_blocked_from_publication": credentials_blocked,
            }
        )

    formal: list[tuple[PlaylistEntry, dict[str, str]]] = []
    selected_urls: set[str] = set()
    for channel_id, candidates in publishable_by_channel.items():
        ranked = sorted(candidates, key=lambda item: score_probe(item[1]), reverse=True)
        entry, effective_probe, publish_status = ranked[0]
        selected_urls.add(entry.url)
        formal.append(
            (
                entry,
                {
                    "x-probe-status": publish_status,
                    "x-height": str(effective_probe.get("height", 0)),
                    "x-score": str(score_probe(effective_probe)),
                },
            )
        )

    for entry, detail in zip(raw_entries, report_entries):
        if entry.url in selected_urls:
            continue
        experimental.append(
            (
                entry,
                {
                    "x-probe-status": detail["probe"]["status"],
                    "x-failure-count": str(detail["consecutive_failures"]),
                },
            )
        )

    formal.sort(key=lambda item: (item[0].attrs.get("x-category", ""), item[0].attrs.get("x-channel-id", "")))
    experimental.sort(
        key=lambda item: (
            item[0].attrs.get("x-review-tier", ""),
            item[0].attrs.get("x-category", ""),
            item[0].attrs.get("x-channel-id", ""),
        )
    )
    write_playlist(args.formal, formal)
    write_playlist(args.experimental, experimental)
    write_json(args.state, state)
    ensure_epg_placeholder(args.epg)
    report = {
        "generated_at": checked_at,
        "ffprobe": ffprobe,
        "raw_entries": len(raw_entries),
        "probed_entries": len(selected_for_probe),
        "formal_entries": len(formal),
        "experimental_entries": len(experimental),
        "probe_statuses": dict(Counter(item["probe"]["status"] for item in report_entries)),
        "entries": report_entries,
    }
    write_json(args.report, report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--formal", type=Path, default=DEFAULT_FORMAL)
    parser.add_argument("--experimental", type=Path, default=DEFAULT_EXPERIMENTAL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--epg", type=Path, default=DEFAULT_EPG)
    parser.add_argument("--ffprobe")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--proxy-mode",
        choices=("direct", "environment"),
        default="direct",
        help="Use direct ffprobe connections by default; opt into HTTP_PROXY/HTTPS_PROXY with environment.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Probe only the first N eligible entries for a smoke run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build(args)
    print(
        f"Probed {report['probed_entries']} of {report['raw_entries']} candidates; "
        f"wrote {report['formal_entries']} formal and {report['experimental_entries']} experimental entries."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
