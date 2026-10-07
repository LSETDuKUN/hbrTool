"""Portrait-only asset cache, independent from numeric data snapshots."""
import concurrent.futures
import hashlib
import io
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from .store import Catalog
from .sync import ROOT, fetch


def asset_urls(region, dataset, filename):
    """Site's CDN/fallback convention; enemy files are candidates, not assumed present."""
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        return []
    name = quote(filename)
    family = "chara_icon" if dataset == "characters" else "card"
    prefix = "en/" if region == "en" else ""
    urls = [f"https://cdn.hbr.quest/webp/{region}/{family}/{name}",
            f"https://assets.hbr.quest/v1/{prefix}hbr/{name}"]
    if dataset == "enemies":
        urls.insert(0, f"https://cdn.hbr.quest/webp/{region}/enemy/{name}")
    return urls


class AssetStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.index_path = self.root / "portraits.json"

    def index(self):
        if not self.index_path.exists():
            return {"schema": 1, "records": {}, "urls": {}}
        return json.loads(self.index_path.read_bytes())

    def get(self, region, dataset, entity_id):
        """Stable lookup API: status, source URL, SHA256, local path, source filename."""
        entry = self.index()["records"].get(f"{region}/{dataset}/{entity_id}")
        if entry is None:
            return None
        result = dict(entry)
        result["local_path"] = str(self.root / entry["path"]) if entry.get("path") else None
        return result

    def paths(self, region, dataset, entity_ids):
        records = self.index()["records"]
        return {entity_id:str(self.root/records[f"{region}/{dataset}/{entity_id}"]["path"])
                for entity_id in entity_ids
                if records.get(f"{region}/{dataset}/{entity_id}",{}).get("path")}


@contextmanager
def asset_lock(root):
    """OS-owned lock is released even if the GUI process exits during a download."""
    root.mkdir(parents=True,exist_ok=True)
    handle = (root/"portraits.lock").open("a+b")
    try:
        if handle.seek(0,2) == 0:
            handle.write(b"0");handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("头像同步正在进行，请等待当前任务完成") from exc
        yield
    finally:
        handle.close()


def sync_portraits(root=ROOT, progress=print, retry_missing=False):
    with asset_lock(Path(root)):
        return _sync_portraits(root,progress,retry_missing)


def _sync_portraits(root, progress, retry_missing):
    from PIL import Image
    root = Path(root)
    store = AssetStore(root)
    index = store.index()
    catalog = Catalog(root)
    _, rows, truncated = catalog.query(
        "SELECT region,dataset,id,json_extract(raw_json,'$.image') FROM records "
        "WHERE dataset IN ('styles','characters','enemies')", limit=100000, seconds=15)
    if truncated:
        raise ValueError("头像索引超过上限，请更新采集器；未发布截断索引")
    objects = root / "portraits"
    objects.mkdir(exist_ok=True)
    jobs = {}
    for region, dataset, entity_id, filename in rows:
        urls = asset_urls(region,dataset,filename)
        if urls:
            jobs.setdefault(tuple(urls), []).append((region,dataset,entity_id,filename))
    results = dict(index["urls"])

    def download(urls):
        attempts = []
        for url in urls:
            cached = index["urls"].get(url)
            if cached and (cached.get("path") and (root/cached["path"]).exists() or
                           cached.get("status") == "missing" and not retry_missing):
                result = cached
            else:
                try:
                    data = fetch(url)
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                    digest = hashlib.sha256(data).hexdigest()
                    path = objects / (digest+".webp")
                    if not path.exists():
                        path.write_bytes(data)
                    result = {"status":"ready", "url":url, "sha256":digest,
                              "path":path.relative_to(root).as_posix()}
                except Exception as exc:
                    result = {"status":"missing" if "HTTP Error 404:" in str(exc) else "error",
                              "url":url,"error":str(exc)}
            attempts.append(result)
            if result["status"] == "ready":
                return urls,result,attempts
        status = "error" if any(a["status"] == "error" for a in attempts) else "missing"
        return urls,{"status":status, "attempts":attempts},attempts

    def save():
        index["urls"] = results
        temp = store.index_path.with_suffix(".tmp")
        temp.write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding="utf-8")
        # Windows may briefly deny replacement while a query window reads the index.
        for attempt in range(20):
            try:
                temp.replace(store.index_path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.1)

    # Main thread owns index mutations. Identical filenames are fetched only once per region.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for count,(urls,result,attempts) in enumerate(pool.map(download,jobs),1):
            for attempt in attempts:
                results[attempt["url"]] = attempt
            for region,dataset,entity_id,filename in jobs[urls]:
                index["records"][f"{region}/{dataset}/{entity_id}"] = dict(result,filename=filename)
            if count % 40 == 0:
                save()
                progress(f"头像资源 {count}/{len(jobs)}")
    index["snapshot"] = catalog.manifest()["database"]
    save()
    progress(f"头像资源完成：{len(jobs)} 组文件地址；原图和失败原因均已保存")
    return index


if __name__ == "__main__":
    sync_portraits()
