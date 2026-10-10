import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hbr_data.store import Catalog, build_database
from hbr_data.sync import sync


class QuestDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill = {"id": 12, "label": "test", "name": "Test", "hit_count": 3,
                      "sp_cost": 8, "max_level": 10, "future_field": {"keep": True},
                      "parts": [{"skill_type": "AttackSkill", "power": [100.5, 301.25],
                                 "diff_for_max": 123, "multipliers": {"dp": 1, "hp": 1.3, "dr": 2.5},
                                 "parameters": {"str": 2, "dex": 1}, "growth": [.05,.02],
                                 "hits": [{"weight": .3}, {"weight": .7}]},
                                {"skill_type": "Heal", "power": [12, 20], "cond": "conditional"}]}
        self.manifest = {"database": "test.sqlite3", "sources": {}}
        self.add_source("jp", "skills", [self.skill])
        self.add_source("cn", "skills", [dict(self.skill, name="测试", parts=[])])
        self.add_source("jp", "styles", [{"id": 21, "label": "style", "skills": [dict(self.skill, name="Variant")]}])
        self.add_source("jp", "localization", {"skill":{"name":{"test":"測試"}}})
        build_database(self.root, self.manifest, self.root / "test.sqlite3")
        (self.root / "current.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.catalog = Catalog(self.root)

    def add_source(self, region, table, data):
        raw = json.dumps(data).encode()
        name = hashlib.sha256(raw).hexdigest()+".json"
        (self.root / name).write_bytes(raw)
        url = "https://example/zhTW.json" if table == "localization" else f"https://example/{region}/{table}.json"
        self.manifest["sources"][url] = {
            "region":region, "table":table, "path":name, "fetched":"now"}

    def test_values_keep_part_boundaries_and_unknowns(self):
        columns, rows, _ = self.catalog.query("SELECT hit_count,power_min,power_max,diff_for_max,hp_multiplier,destruction_multiplier FROM skill_damage WHERE region='jp' ORDER BY part_index")
        self.assertEqual(rows[0], (3,100.5,301.25,123,1.3,2.5))
        self.assertIsNone(rows[1][-1])
        raw = self.catalog.query("SELECT raw_json FROM skill_catalog WHERE region='jp'")[1][0][0]
        self.assertEqual(json.loads(raw), self.skill)
        self.assertEqual(self.catalog.query("SELECT count(*) FROM skill_hits")[1][0][0], 4)

    def test_variants_and_servers_do_not_overwrite_each_other(self):
        self.assertEqual(self.catalog.query("SELECT count(*) FROM skills")[1][0][0], 3)
        self.assertEqual(self.catalog.query("SELECT count(*) FROM skill_catalog")[1][0][0], 2)
        self.assertEqual(self.catalog.query("SELECT name_zh FROM records WHERE dataset='skills' AND region='jp'")[1][0][0], "測試")

    def test_readonly_sql_rejects_mutation_attach_and_pragma(self):
        for sql in ["DELETE FROM skills", "DROP TABLE skills", "ATTACH DATABASE ':memory:' AS other", "PRAGMA user_version=6", "SELECT load_extension('x')"]:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.catalog.query(sql)
        self.assertEqual(self.catalog.query("SELECT count(*) FROM skills")[1][0][0], 3)

    def test_query_limits_time_and_rows(self):
        self.assertTrue(self.catalog.query("SELECT * FROM skills", limit=1)[2])
        with self.assertRaises(sqlite3.OperationalError):
            self.catalog.query("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n", seconds=.01)

    def test_integrity_failure_does_not_accept_changed_blob(self):
        source = next(iter(self.manifest["sources"].values()))
        (self.root / source["path"]).write_text("[]")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            build_database(self.root, self.manifest, self.root / "broken.sqlite3")

    def test_core_failure_keeps_published_database_and_unlocks(self):
        before = (self.root / "current.json").read_bytes()
        with patch("hbr_data.sync.discover", return_value=(["characters","styles","skills"], {})), patch("hbr_data.sync.fetch", side_effect=OSError("offline")):
            with self.assertRaisesRegex(RuntimeError, "旧资料库"):
                sync(self.root, progress=lambda _: None)
        self.assertEqual((self.root / "current.json").read_bytes(), before)
        self.assertFalse((self.root / "sync.lock").exists())


if __name__ == "__main__":
    unittest.main()
