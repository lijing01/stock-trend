#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core.kline_cache import (  # noqa: E402
    KlineCoverage,
    KlineIdentity,
    clean_kline_cache,
    get_kline_cache_path,
    inventory_legacy_kline_cache,
    load_best_kline_cache,
    load_kline_cache,
    migrate_legacy_kline_cache,
    publish_kline_cache,
)


def payload(days, *, source="eastmoney", latest=20260131,
            ts_code="600519.SH", asset="E", adj="qfq"):
    latest_day = datetime.strptime(str(latest), "%Y%m%d")
    rows = []
    for offset in range(days):
        day = latest_day - timedelta(days=days - offset - 1)
        close = 10 + offset
        rows.append({
            "trade_date": day.strftime("%Y%m%d"), "open": close - 0.2,
            "high": close + 0.3, "low": close - 0.4, "close": close,
            "vol": 1000 + offset,
        })
    return {
        "meta": {
            "ts_code": ts_code,
            "asset": asset,
            "freq": "D",
            "adj": adj,
            "record_count": len(rows),
            "data_source": source,
        },
        "data": rows,
    }


class KlineCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tempdir.name)
        self.identity = KlineIdentity("600519.sh", "e", "d", "qfq", "eastmoney")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_identity_is_complete_and_path_is_namespaced(self):
        path = get_kline_cache_path(self.identity, self.cache_dir)
        self.assertEqual(
            path.relative_to(self.cache_dir).as_posix(),
            "klines/v1/600519.SH/E/D/qfq/eastmoney.json",
        )
        with self.assertRaises(ValueError):
            KlineIdentity("../../escape", "E", "D", "qfq", "eastmoney")

    def test_publish_and_load_preserve_payload_contract_and_crop(self):
        original = payload(10)
        self.assertTrue(publish_kline_cache(self.identity, original, cache_dir=self.cache_dir))
        loaded = load_kline_cache(
            self.identity,
            KlineCoverage(min_records=8, limit=3),
            cache_dir=self.cache_dir,
        )
        self.assertEqual(set(loaded), {"meta", "data"})
        self.assertEqual(len(loaded["data"]), 3)
        self.assertEqual(loaded["meta"]["record_count"], 3)
        document = json.loads(get_kline_cache_path(self.identity, self.cache_dir).read_text())
        self.assertEqual(document["kline_cache"]["identity"], self.identity.as_dict())

    def test_incomplete_or_expired_series_is_a_miss(self):
        self.assertTrue(
            publish_kline_cache(
                self.identity, payload(6), cache_dir=self.cache_dir, fetched_at=100
            )
        )
        self.assertIsNone(
            load_kline_cache(
                self.identity, KlineCoverage(min_records=7), cache_dir=self.cache_dir
            )
        )
        self.assertIsNone(
            load_kline_cache(
                self.identity,
                KlineCoverage(ttl_seconds=10),
                cache_dir=self.cache_dir,
                now=111,
            )
        )

    def test_incomplete_ohlcv_rows_are_not_publishable(self):
        incomplete = payload(1)
        incomplete["data"][0].pop("open")
        self.assertFalse(
            publish_kline_cache(self.identity, incomplete, cache_dir=self.cache_dir)
        )

    def test_refresh_cannot_drop_earliest_coverage(self):
        self.assertTrue(publish_kline_cache(
            self.identity, payload(3, latest=20260103), cache_dir=self.cache_dir))
        self.assertFalse(publish_kline_cache(
            self.identity, payload(3, latest=20260104), cache_dir=self.cache_dir))
        loaded = load_kline_cache(self.identity, cache_dir=self.cache_dir)
        self.assertEqual(loaded["data"][0]["trade_date"], "20260101")

    def test_publish_rejects_partial_or_duplicate_rows(self):
        invalid = payload(5)
        invalid["data"].append({"trade_date": "20260106", "close": 1})
        self.assertFalse(publish_kline_cache(self.identity, invalid, cache_dir=self.cache_dir))
        duplicate = payload(5)
        duplicate["data"].append(dict(duplicate["data"][-1]))
        self.assertFalse(publish_kline_cache(self.identity, duplicate, cache_dir=self.cache_dir))

    def test_requested_range_with_no_bars_is_a_miss(self):
        stored = payload(2)
        self.assertTrue(publish_kline_cache(self.identity, stored, cache_dir=self.cache_dir))
        self.assertIsNone(load_kline_cache(
            self.identity,
            KlineCoverage(start_date="20260110", end_date="20260120"),
            cache_dir=self.cache_dir,
        ))

    def test_shorter_or_older_refresh_does_not_replace_good_cache(self):
        self.assertTrue(publish_kline_cache(self.identity, payload(10), cache_dir=self.cache_dir))
        self.assertFalse(publish_kline_cache(self.identity, payload(5), cache_dir=self.cache_dir))
        self.assertFalse(
            publish_kline_cache(
                self.identity, payload(10, latest=20260130), cache_dir=self.cache_dir
            )
        )
        loaded = load_kline_cache(self.identity, cache_dir=self.cache_dir)
        self.assertEqual(len(loaded["data"]), 10)
        self.assertEqual(loaded["data"][-1]["trade_date"], "20260131")

    def test_error_payload_never_replaces_success_and_no_temp_is_left(self):
        self.assertTrue(publish_kline_cache(self.identity, payload(4), cache_dir=self.cache_dir))
        error = {"meta": {"data_source": "error"}, "data": []}
        self.assertFalse(publish_kline_cache(self.identity, error, cache_dir=self.cache_dir))
        self.assertEqual(len(load_kline_cache(self.identity, cache_dir=self.cache_dir)["data"]), 4)
        self.assertEqual(list(self.cache_dir.rglob("*.tmp")), [])

    def test_payload_source_must_match_actual_source_identity(self):
        wrong = KlineIdentity("600519.SH", "E", "D", "qfq", "baostock")
        self.assertFalse(publish_kline_cache(wrong, payload(4), cache_dir=self.cache_dir))
        self.assertFalse(get_kline_cache_path(wrong, self.cache_dir).exists())

    def test_best_cache_respects_source_preference(self):
        second = KlineIdentity("600519.SH", "E", "D", "qfq", "baostock")
        self.assertTrue(
            publish_kline_cache(
                second, payload(5, source="baostock"), cache_dir=self.cache_dir
            )
        )
        hit = load_best_kline_cache(
            [self.identity, second], KlineCoverage(min_records=5), cache_dir=self.cache_dir
        )
        self.assertEqual(hit[1], second)

    def test_fallback_and_hk_identities_round_trip_through_best_loader(self):
        cases = [
            (
                "600519.SH", "E", "qfq", "tencent_a",
                ("eastmoney", "tencent_a", "baostock"),
            ),
            (
                "00700.HK", "E", "qfq", "tencent_hk",
                ("tencent_hk",),
            ),
        ]
        for ts_code, asset, adj, source, sources in cases:
            with self.subTest(source=source):
                identity = KlineIdentity(ts_code, asset, "D", adj, source)
                stored = payload(
                    5, source=source, ts_code=ts_code,
                    asset=asset, adj=adj)
                self.assertTrue(publish_kline_cache(
                    identity, stored, cache_dir=self.cache_dir))
                hit = load_best_kline_cache(
                    [KlineIdentity(ts_code, asset, "D", adj, item)
                     for item in sources],
                    KlineCoverage(min_records=5),
                    cache_dir=self.cache_dir,
                )
                self.assertEqual(hit[0], stored)
                self.assertEqual(hit[1], identity)

    def test_cleanup_only_evicts_managed_files_and_honors_protection(self):
        first = self.identity
        second = KlineIdentity("000001.SZ", "E", "D", "qfq", "eastmoney")
        second_payload = payload(40)
        second_payload["meta"]["ts_code"] = "000001.SZ"
        self.assertTrue(publish_kline_cache(first, payload(40), cache_dir=self.cache_dir, fetched_at=1))
        self.assertTrue(publish_kline_cache(second, second_payload, cache_dir=self.cache_dir, fetched_at=2))
        unrelated = self.cache_dir / "market_regime.json"
        unrelated.write_text('{"keep": true}', encoding="utf-8")
        protected = get_kline_cache_path(first, self.cache_dir)
        result = clean_kline_cache(0, cache_dir=self.cache_dir, protected_paths=[protected])
        self.assertTrue(protected.exists())
        self.assertFalse(get_kline_cache_path(second, self.cache_dir).exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(result["within_budget"])

    def _legacy_file(self, name="kline_600519.SH_D_qfq.json", *, doc=None) -> Path:
        path = self.cache_dir / name
        path.write_text(
            json.dumps(doc if doc is not None else payload(6)), encoding="utf-8"
        )
        return path

    def test_inventory_reports_would_migrate_without_side_effects(self):
        legacy = self._legacy_file()
        reports = inventory_legacy_kline_cache(
            [legacy],
            identity=self.identity,
            cache_dir=self.cache_dir,
            meta_overrides={
                "ts_code": self.identity.ts_code,
                "asset": self.identity.asset,
                "freq": self.identity.freq,
                "adj": self.identity.adj,
                "data_source": self.identity.source,
            },
        )
        self.assertEqual(reports[0]["status"], "would_migrate")
        self.assertTrue(legacy.exists())
        self.assertFalse(get_kline_cache_path(self.identity, self.cache_dir).exists())

    def test_migrate_promotes_and_backs_up_legacy_file(self):
        backup_dir = self.cache_dir / "backup"
        legacy = self._legacy_file()
        report = migrate_legacy_kline_cache(
            legacy,
            identity=self.identity,
            cache_dir=self.cache_dir,
            backup_dir=backup_dir,
            meta_overrides={
                "ts_code": self.identity.ts_code,
                "asset": self.identity.asset,
                "freq": self.identity.freq,
                "adj": self.identity.adj,
                "data_source": self.identity.source,
            },
        )
        self.assertEqual(report["status"], "migrated")
        self.assertFalse(legacy.exists())
        self.assertTrue(Path(report["backup"]).exists())
        stored = get_kline_cache_path(self.identity, self.cache_dir)
        self.assertTrue(stored.exists())
        loaded = load_kline_cache(self.identity, cache_dir=self.cache_dir)
        self.assertEqual(len(loaded["data"]), 6)

    def test_migrate_does_not_downgrade_existing_cache_and_keeps_legacy(self):
        self.assertTrue(
            publish_kline_cache(self.identity, payload(10), cache_dir=self.cache_dir)
        )
        legacy = self._legacy_file()  # 6 records, so publish would shrink coverage
        backup_dir = self.cache_dir / "backup"
        report = migrate_legacy_kline_cache(
            legacy,
            identity=self.identity,
            cache_dir=self.cache_dir,
            backup_dir=backup_dir,
        )
        self.assertEqual(report["status"], "conflict")
        self.assertTrue(legacy.exists())
        self.assertFalse((backup_dir / legacy.name).exists())
        loaded = load_kline_cache(self.identity, cache_dir=self.cache_dir)
        self.assertEqual(len(loaded["data"]), 10)

    def test_migrate_rejects_invalid_legacy_file(self):
        legacy = self._legacy_file(doc={"meta": {"data_source": "error"}, "data": []})
        report = migrate_legacy_kline_cache(
            legacy,
            identity=self.identity,
            cache_dir=self.cache_dir,
            meta_overrides={
                "ts_code": self.identity.ts_code,
                "asset": self.identity.asset,
                "freq": self.identity.freq,
                "adj": self.identity.adj,
                "data_source": self.identity.source,
            },
        )
        self.assertEqual(report["status"], "invalid")
        self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
