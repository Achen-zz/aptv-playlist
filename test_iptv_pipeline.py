#!/usr/bin/env python3
"""Unit tests for the IPTV candidate pipeline."""

from __future__ import annotations

import unittest
from unittest import mock
import tempfile

from fetch_raw import (
    PlaylistEntry,
    classify_tier,
    deduplicate,
    load_json,
    parse_m3u,
    target_for_entry,
)
from pathlib import Path
from playlist_builder import ffprobe_environment
from prepare_publish import build_snapshot, ensure_public_entry


ROOT = Path(__file__).resolve().parent


class PlaylistParserTests(unittest.TestCase):
    def test_parser_handles_bom_chinese_comma_and_directive(self) -> None:
        text = (
            "\ufeff#EXTM3U\n"
            '#EXTINF:-1 tvg-name="湖南卫视" group-title="卫视频道",湖南卫视,高清\n'
            "#EXTVLCOPT:http-referrer=https://example.test/\n"
            "https://example.test/live.m3u8\n"
        )
        entries = parse_m3u(text, "fixture", 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "湖南卫视,高清")
        self.assertEqual(entries[0].attrs["tvg-name"], "湖南卫视")
        self.assertEqual(entries[0].directives, ["#EXTVLCOPT:http-referrer=https://example.test/"])

    def test_deduplicate_keeps_higher_priority_source(self) -> None:
        low = PlaylistEntry("-1", {}, "low", "https://example.test/live.m3u8", source_priority=50)
        high = PlaylistEntry("-1", {}, "high", "https://example.test/live.m3u8", source_priority=10)
        entries, removed = deduplicate([low, high])
        self.assertEqual(removed, 1)
        self.assertEqual(entries[0].name, "high")


class ClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_json(ROOT / "channels.json")

    def test_cc_tv_is_not_mistaken_for_cctv(self) -> None:
        entry = PlaylistEntry("-1", {"tvg-id": "CCTV.us@SD"}, "Charlotte County CC-TV (720p)", "https://example.test")
        self.assertIsNone(target_for_entry(entry, self.config["targets"]))

    def test_cctv_and_satellite_are_matched(self) -> None:
        cctv = PlaylistEntry("-1", {"tvg-name": "CCTV1"}, "CCTV-1综合", "https://example.test/cctv")
        satellite = PlaylistEntry("-1", {"tvg-name": "湖南卫视"}, "湖南卫视", "https://example.test/hunan")
        self.assertEqual(target_for_entry(cctv, self.config["targets"])["id"], "CCTV1")
        self.assertEqual(target_for_entry(satellite, self.config["targets"])["id"], "HUNAN-SAT")

    def test_display_name_wins_when_tvg_name_is_ambiguous(self) -> None:
        entry = PlaylistEntry("-1", {"tvg-name": "CCTV5"}, "CCTV-5+体育赛事", "https://example.test/cctv5plus")
        self.assertEqual(target_for_entry(entry, self.config["targets"])["id"], "CCTV5PLUS")

    def test_french_france24_is_not_matched_as_english(self) -> None:
        entry = PlaylistEntry(
            "-1",
            {"tvg-name": "France 24 Ⓨ", "tvg-id": "France24French.fr"},
            "France 24 Ⓨ",
            "https://example.test/france24-fr",
        )
        self.assertIsNone(target_for_entry(entry, self.config["targets"]))

    def test_english_france24_resolution_suffix_is_matched(self) -> None:
        entry = PlaylistEntry(
            "-1",
            {"tvg-id": "France24.fr@English"},
            "France 24 English (1080p)",
            "https://example.test/france24-en",
        )
        self.assertEqual(target_for_entry(entry, self.config["targets"])["id"], "FRANCE24-ENGLISH")

    def test_premium_and_unavailable_ipv6_tiers(self) -> None:
        source = {"default_tier": "review-required"}
        cnn = {"premium": True}
        regular = {"premium": False}
        ipv6 = PlaylistEntry("-1", {}, "ipv6", "http://[2409:8087::1]/live.m3u8")
        ipv4 = PlaylistEntry("-1", {}, "ipv4", "https://example.test/live.m3u8")
        self.assertEqual(classify_tier(ipv4, cnn, source, False)[0], "experimental-premium")
        self.assertEqual(classify_tier(ipv6, regular, source, False)[0], "ipv6-unavailable")

    def test_curated_official_url_prefix_is_approved(self) -> None:
        source = {
            "default_tier": "review-required",
            "approved_url_prefixes": ["https://official.example.test/live/"],
        }
        target = {"premium": False}
        entry = PlaylistEntry("-1", {}, "official", "https://official.example.test/live/master.m3u8")
        self.assertEqual(classify_tier(entry, target, source, False)[0], "approved-candidate")


class ProbeEnvironmentTests(unittest.TestCase):
    def test_direct_mode_removes_proxy_variables(self) -> None:
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:7897"}, clear=True):
            self.assertNotIn("HTTP_PROXY", ffprobe_environment("direct"))
            self.assertEqual(ffprobe_environment("environment")["HTTP_PROXY"], "http://127.0.0.1:7897")


class PublishSnapshotTests(unittest.TestCase):
    def test_sensitive_url_is_rejected(self) -> None:
        entry = PlaylistEntry(
            "-1",
            {"x-review-tier": "approved-candidate"},
            "protected",
            "https://example.test/live.m3u8?token=secret",
        )
        with self.assertRaises(ValueError):
            ensure_public_entry(entry)

    def test_expanded_playlist_allows_review_but_rejects_premium(self) -> None:
        review = PlaylistEntry(
            "-1",
            {"x-review-tier": "review-required"},
            "review",
            "https://example.test/review.m3u8",
        )
        premium = PlaylistEntry(
            "-1",
            {"x-review-tier": "experimental-premium"},
            "premium",
            "https://example.test/premium.m3u8",
        )
        ensure_public_entry(review, {"approved-candidate", "review-required"})
        with self.assertRaises(ValueError):
            ensure_public_entry(premium, {"approved-candidate", "review-required"})

    def test_snapshot_contains_only_public_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp = Path(temp_dir)
            playlist = temp / "playlist.m3u"
            epg = temp / "epg.xml"
            output = temp / "public"
            playlist.write_text(
                "#EXTM3U\n"
                '#EXTINF:-1 x-review-tier="approved-candidate" x-channel-id="NEWS" '
                'x-height="1080" x-category="News",News\n'
                "https://example.test/live.m3u8\n",
                encoding="utf-8",
            )
            epg.write_text('<?xml version="1.0" encoding="utf-8"?><tv />', encoding="utf-8")
            status = build_snapshot(playlist, epg, output)
            self.assertEqual(status["channel_count"], 1)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    ".nojekyll",
                    "epg.xml",
                    "index.html",
                    "playlist-expanded.m3u",
                    "playlist-ipv6.m3u",
                    "playlist.m3u",
                    "status.json",
                },
            )


if __name__ == "__main__":
    unittest.main()
