"""Lossless JSON records plus typed skill/effect projections (schema version 1)."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from .sync import ROOT

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE sources(url TEXT PRIMARY KEY, region TEXT NOT NULL, dataset TEXT NOT NULL,
 path TEXT NOT NULL, fetched TEXT, bytes INTEGER, sha256 TEXT NOT NULL);
CREATE TABLE records(record_key TEXT PRIMARY KEY, source_url TEXT REFERENCES sources(url),
 region TEXT, dataset TEXT, ordinal INTEGER, id TEXT, label TEXT, name TEXT, name_zh TEXT,
 raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE translations(language TEXT, category TEXT, field TEXT, label TEXT, text TEXT,
 PRIMARY KEY(language,category,field,label));
CREATE INDEX records_lookup ON records(region,dataset,name);
CREATE TABLE skills(skill_key TEXT PRIMARY KEY, source_url TEXT REFERENCES sources(url),
 json_pointer TEXT, region TEXT, dataset TEXT, id TEXT, label TEXT, name TEXT,
 description TEXT, hit_count INTEGER, sp_cost REAL, max_level INTEGER, target TEXT,
 raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE INDEX skills_lookup ON skills(region,dataset,name);
CREATE INDEX skills_identity ON skills(region,id);
CREATE TABLE skill_parts(skill_key TEXT REFERENCES skills(skill_key), part_index INTEGER,
 skill_type TEXT, target TEXT, physical_type TEXT, power_min REAL, power_max REAL,
 diff_for_max REAL, dp_multiplier REAL, hp_multiplier REAL, destruction_multiplier REAL,
 str_weight REAL, dex_weight REAL, wis_weight REAL, spr_weight REAL, luk_weight REAL, con_weight REAL,
 condition TEXT, elements_json TEXT, growth_json TEXT, hits_json TEXT, raw_json TEXT NOT NULL,
 PRIMARY KEY(skill_key,part_index));
CREATE TABLE skill_hits(skill_key TEXT REFERENCES skills(skill_key), part_index INTEGER,
 hit_index INTEGER, hit_id INTEGER, hit_type TEXT, power_ratio REAL, raw_json TEXT NOT NULL,
 PRIMARY KEY(skill_key,part_index,hit_index));
CREATE TABLE skill_elements(skill_key TEXT, part_index INTEGER, element_index INTEGER, element TEXT,
 PRIMARY KEY(skill_key,part_index,element_index),
 FOREIGN KEY(skill_key,part_index) REFERENCES skill_parts(skill_key,part_index));
CREATE VIEW characters AS SELECT * FROM records WHERE dataset='characters';
CREATE VIEW styles AS SELECT * FROM records WHERE dataset='styles';
CREATE VIEW enemies AS SELECT * FROM records WHERE dataset='enemies';
CREATE VIEW skill_catalog AS SELECT * FROM skills WHERE dataset='skills';
CREATE VIEW skill_damage AS SELECT s.region,s.id,s.name,s.hit_count,s.sp_cost,p.*
 FROM skill_catalog s JOIN skill_parts p USING(skill_key);
CREATE VIEW style_skills AS SELECT r.region,r.id AS style_id,r.label AS style_label,
 s.id AS skill_id,s.skill_key,s.json_pointer
 FROM styles r JOIN skills s ON s.source_url=r.source_url
 AND s.json_pointer LIKE '/' || r.ordinal || '/skills/%';
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def walk(value, pointer=""):
    if isinstance(value, dict):
        yield pointer, value
        for key, child in value.items():
            yield from walk(child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, pointer + f"/{index}")


def build_database(root, manifest, destination):
    with closing(sqlite3.connect(destination)) as db, db:
        db.executescript(SCHEMA)
        localize = {}
        for url, source in manifest["sources"].items():
            if source["table"] != "localization":
                continue
            language = url.rsplit("/",1)[-1].split(".")[0]
            localized = json.loads((Path(root) / source["path"]).read_bytes())
            if language == "zhTW":
                localize = localized
            for category, fields in localized.items():
                for field, labels in fields.items():
                    if isinstance(labels, dict):
                        db.executemany("INSERT INTO translations VALUES (?,?,?,?,?)", [
                            (language,category,field,label,text if isinstance(text,str) else encode(text))
                            for label,text in labels.items()])
        for url, source in manifest["sources"].items():
            region, dataset = source["region"], source["table"]
            raw = (Path(root) / source["path"]).read_bytes()
            import hashlib
            digest = hashlib.sha256(raw).hexdigest()
            if Path(source["path"]).stem != digest:
                raise ValueError(f"快照校验失败: {url}")
            data = json.loads(raw)
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                       (url, region, dataset, source["path"], source["fetched"], len(raw), digest))
            items = data if isinstance(data, list) else [data]
            for index, item in enumerate(items):
                fields = item if isinstance(item, dict) else {}
                category = {"skills":"skill", "styles":"card", "enemies":"enemy", "passives":"skill"}.get(dataset)
                translated = localize.get(category, {}).get("name", {}).get(fields.get("label")) if region == "jp" else None
                if region == "jp" and dataset == "characters":
                    translated = "".join(localize.get("character",{}).get(field,{}).get(fields.get("label"),"") for field in ("lastname","firstname")) or None
                db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    f"{url}#/{index}", url, region, dataset, index,
                    str(fields.get("id", "")), str(fields.get("label", "")),
                    str(fields.get("name", "")), translated, encode(item)))
            for pointer, skill in walk(data):
                if "hit_count" not in skill or not isinstance(skill.get("parts"), list):
                    continue
                key = url + "#" + pointer
                db.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    key, url, pointer, region, dataset, str(skill.get("id", "")),
                    skill.get("label"), skill.get("name"), skill.get("desc"),
                    skill.get("hit_count"), skill.get("sp_cost"), skill.get("max_level"),
                    skill.get("target_type"), encode(skill)))
                for part_index, part in enumerate(skill["parts"]):
                    powers = part.get("power") or []
                    multipliers = part.get("multipliers") or {}
                    parameters = part.get("parameters") or {}
                    values = (key, part_index, part.get("skill_type"), part.get("target_type"),
                              part.get("type"), powers[0] if powers else None,
                              powers[1] if len(powers) > 1 else None, part.get("diff_for_max"),
                              multipliers.get("dp"), multipliers.get("hp"), multipliers.get("dr"),
                              *(parameters.get(stat) for stat in ("str", "dex", "wis", "spr", "luk", "con")),
                              part.get("cond"), encode(part.get("elements")), encode(part.get("growth")),
                              encode(part.get("hits")), encode(part))
                    db.execute("INSERT INTO skill_parts VALUES (" + ",".join("?" for _ in values) + ")", values)
                    for i, element in enumerate(part.get("elements") or []):
                        db.execute("INSERT INTO skill_elements VALUES (?,?,?,?)", (key,part_index,i,element))
                    for i, hit in enumerate(part.get("hits") or []):
                        fields = hit if isinstance(hit,dict) else {}
                        db.execute("INSERT INTO skill_hits VALUES (?,?,?,?,?,?,?)", (key, part_index, i, fields.get("id"),fields.get("type"),fields.get("power_ratio"),encode(hit)))
                for i, hit in enumerate(skill.get("hits") or []):
                    fields = hit if isinstance(hit,dict) else {}
                    db.execute("INSERT INTO skill_hits VALUES (?,?,?,?,?,?,?)", (key, -1, i,fields.get("id"),fields.get("type"),fields.get("power_ratio"),encode(hit)))
        db.execute("PRAGMA user_version=1")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("资料库关联校验失败")
        db.execute("ANALYZE")


class Catalog:
    def __init__(self, root=ROOT):
        self.root = Path(root)

    def manifest(self):
        return json.loads((self.root / "current.json").read_text(encoding="utf-8"))

    def query(self, sql, params=(), limit=500, seconds=3):
        path = self.root / self.manifest()["database"]
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        deadline = time.monotonic() + seconds
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        # An allowlist rejects ATTACH, PRAGMA, DDL, writes and extension loading,
        # including side effects disguised in otherwise read-only statements.
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        def authorize(action, arg1, arg2, database, trigger):
            if action not in allowed or (action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {"load_extension", "writefile", "readfile"}):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        try:
            cursor = db.execute(sql, params)
            rows = cursor.fetchmany(limit + 1)
            return [column[0] for column in cursor.description], rows[:limit], len(rows) > limit
        finally:
            db.close()

    def search(self, region, dataset, keyword="", limit=500):
        pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return self.query("SELECT record_key,id,name,label FROM records WHERE region=? AND dataset=? "
                          "AND (name LIKE ? ESCAPE '\\' OR label LIKE ? ESCAPE '\\' OR id=? OR raw_json LIKE ? ESCAPE '\\') ORDER BY ordinal",
                          (region, dataset, pattern, pattern, keyword, pattern), limit)

    def skill_variants(self, region, skill_id):
        """Return source-tagged variants, never pick an arbitrary region/version."""
        _, rows, _ = self.query("SELECT skill_key,json_pointer,raw_json FROM skill_catalog WHERE region=? AND id=?", (region,str(skill_id)))
        return [{"skill_key": key, "json_pointer": pointer, "data": json.loads(raw)} for key,pointer,raw in rows]
