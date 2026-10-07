"""Public data acquisition. Original bytes are content-addressed; no guessed IDs."""
import concurrent.futures
import hashlib
import http.client
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "quest"
BASE = "https://master.hbr.quest/v1/"
REGIONS = {"jp": "", "cn": "cn/", "en": "en/"}
_connections = threading.local()


def fetch(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"hbr.quest", "master.hbr.quest"}:
        raise ValueError("仅允许已配置的公开数据站点")
    headers = {"User-Agent": "Mozilla/5.0 HbrTool/1.0", "Accept": "*/*", "Accept-Encoding": "gzip"}
    for attempt in range(3):
        try:
            connections = getattr(_connections, "items", {})
            _connections.items = connections
            connection = connections.setdefault(parsed.hostname, http.client.HTTPSConnection(parsed.hostname, timeout=40))
            connection.request("GET", parsed.path + ("?"+parsed.query if parsed.query else ""), headers=headers)
            response = connection.getresponse()
            data = response.read()
            if response.status != 200:
                raise urllib.error.HTTPError(url, response.status, response.reason, response.headers, None)
            return gzip.decompress(data) if response.getheader("Content-Encoding") == "gzip" else data
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(min(60, max(2, int(exc.headers.get("Retry-After", "10")))))
                continue
            if exc.code < 500:
                raise
        except (OSError, TimeoutError, http.client.HTTPException):
            connections.pop(parsed.hostname, None)
            if attempt == 2:
                raise
        time.sleep(attempt + 1)
    raise OSError(f"下载失败: {url}")


def discover():
    """Follow the site's linked JS modules, then extract actual table references."""
    html = fetch("https://hbr.quest/").decode()
    pending = set(re.findall(r'src="(/assets/[^" ]+\.js)"', html))
    scripts = {}
    while pending:
        path = pending.pop()
        if path in scripts:
            continue
        text = fetch("https://hbr.quest" + path).decode()
        scripts[path] = text
        for name in re.findall(r'["\'](?:\./|assets/)([^"\']+\.js)["\']', text):
            next_path = "/assets/" + name
            if next_path not in scripts:
                pending.add(next_path)
    text = "\n".join(scripts.values())
    tables = set(re.findall(r'\btable:"([a-z_]+)"', text))
    tables.update(re.findall(r'getData\("([a-z_]+)"', text))
    tables.update(re.findall(r'scheduleTable:"([a-z_]+)"', text))
    if not {"characters", "styles", "skills", "enemies"} <= tables:
        raise ValueError("网站结构已变化，未发现核心数据表；保留旧版本")
    return sorted(tables), scripts


def sync(root=ROOT, progress=print, resume=False):
    """Stage downloads, build a new DB, then atomically publish its manifest.

    A failed source is recorded, never silently replaced with stale data.
    Resume explicitly reuses only this incomplete staging manifest.
    """
    from .store import build_database
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "sync.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError("已有同步任务；若进程异常退出，请确认后移除 data/quest/sync.lock")
    try:
        progress("发现公开数据表…")
        stage = root / "staging.json"
        previous = json.loads(stage.read_text(encoding="utf-8")) if resume and stage.exists() else None
        if previous:
            tables = previous["tables"]
            scripts = {url: (root/path).read_text(encoding="utf-8") for url,path in previous["scripts"].items()}
        else:
            tables, scripts = discover()
        manifest = {"schema": 1, "created": datetime.now(timezone.utc).isoformat(),
                    "tables": tables, "sources": {}, "errors": {}, "scripts": {}}
        if previous:
            manifest = previous
        objects = root / "objects"
        objects.mkdir(exist_ok=True)

        def blob(data, suffix):
            digest = hashlib.sha256(data).hexdigest()
            path = objects / (digest + suffix)
            if not path.exists():
                path.write_bytes(data)
            return str(path.relative_to(root)).replace("\\", "/")

        for path, text in scripts.items():
            manifest["scripts"][path if path.startswith("https:") else "https://hbr.quest" + path] = blob(text.encode(), ".js")
        mutex = threading.Lock()
        def save_stage():
            with mutex:
                content = json.dumps(manifest, ensure_ascii=False, indent=2)
            temporary = root / "staging.tmp"
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(stage)

        def download(job):
            region, table, suffix = job
            url = BASE + REGIONS[region] + suffix + ".json"
            if resume and url in manifest["sources"]:
                return
            try:
                data = fetch(url)
                parsed = json.loads(data)
                path = blob(data, ".json")
                entry = {"region": region, "table": table, "path": path,
                         "bytes": len(data), "count": len(parsed) if isinstance(parsed, (list, dict)) else 1,
                         "fetched": datetime.now(timezone.utc).isoformat()}
                with mutex:
                    manifest["sources"][url] = entry
                    manifest["errors"].pop(url, None)
            except Exception as exc:
                with mutex:
                    manifest["errors"][url] = str(exc)

        def batch(jobs, title):
            jobs = sorted(set(jobs))
            if resume:
                jobs = [job for job in jobs if BASE + REGIONS[job[0]] + job[2] + ".json" not in manifest["sources"]]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                for index, _ in enumerate(pool.map(download, jobs), 1):
                    if index % 25 == 0 or index == len(jobs):
                        save_stage()
                        progress(f"{title} {index}/{len(jobs)} · 失败 {len(manifest['errors'])}")

        globals_ = {"translation", "define_values", "battle_config", "levels", "skill_names", "skill_templates", "chapter_names", "mission_types", "special_status_types", "special_statuses"}
        jobs = [(region, table, table) for table in tables for region in REGIONS
                if region == "jp" or (table not in globals_ and not table.startswith("schedule_"))]
        jobs += [("jp", "localization", lang) for lang in ("zhTW", "ko")]
        batch(jobs, "数据表")
        details = []
        for entry in list(manifest["sources"].values()):
            region, table = entry["region"], entry["table"]
            if table not in {"characters", "enemies", "style_reworks", "latest"}:
                continue
            data = json.loads((root / entry["path"]).read_bytes())
            if table == "latest" and isinstance(data, dict):
                for item in (data.get("updated") or {}).get("ls", []):
                    if item.get("label"):
                        details.append((region, "updated_before", "updated/" + item["label"]))
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                label = item.get("label", "")
                if table == "characters" and label:
                    details.append((region, "style_details", "styles/" + label))
                    if item.get("masterly"):
                        details.append((region, "master_skills", "masterSkills/" + label))
                elif table == "enemies" and label and not item.get("skills"):
                    details.append((region, "enemy_details", "enemies/" + label))
                elif table == "style_reworks" and item.get("rework"):
                    details.append((region, "rework_details", "style_reworks/" + item["rework"]))
        batch(details, "详细资料")
        transient = {url:error for url,error in manifest["errors"].items() if "HTTP Error 404:" not in error}
        if transient:
            raise RuntimeError(f"{len(transient)} 个来源发生网络/服务错误；已保留下载进度和旧资料库，可用 --resume 重试")
        for region in REGIONS:
            for table in ("characters", "styles", "skills"):
                if BASE + REGIONS[region] + table + ".json" not in manifest["sources"]:
                    raise RuntimeError(f"缺少核心表 {region}/{table}，未替换现有资料库")
            version_url = BASE + REGIONS[region] + "latest.json"
            if version_url in manifest["sources"]:
                old = json.loads((root / manifest["sources"][version_url]["path"]).read_bytes())
                new = json.loads(fetch(version_url))
                if old.get("version") != new.get("version") or old.get("date") != new.get("date"):
                    raise RuntimeError(f"{region} 站点版本在同步期间变化，请重新同步；旧资料库未替换")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        manifest["database"] = f"catalog-{stamp}.sqlite3"
        progress("建立索引与数值表…")
        build_database(root, manifest, root / manifest["database"])
        save_stage()
        archive = root / f"manifest-{stamp}.json"
        archive.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary = root / "current.tmp"
        temporary.write_bytes(archive.read_bytes())
        temporary.replace(root / "current.json")
        progress(f"完成：{len(manifest['sources'])} 个来源；{len(manifest['errors'])} 个不可用来源（详见覆盖报告）")
        return manifest
    finally:
        lock.rmdir()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    sync(resume=args.resume)
