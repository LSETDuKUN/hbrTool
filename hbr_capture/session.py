"""一次抓帧会话的归档与清理。

场景：用挂件或 watch 跑完一段，`frames/` 里堆了一批帧。
     结束时问一句「这次的结果要留着吗」
       要 -> 整批搬到 `results/<run_id>/`
       不要 -> 整批删掉

因为每条 index 记录里都带 `run` 字段（形如 20261006-121417），
所以能准确认出「哪几帧是这一次跑出来的」，不会误伤别的会话。

搬走/删掉之后，主 `index.jsonl` 里对应的行也会被移除，
不会留下指向不存在文件的死记录。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import List, Optional

from .recycle import recycle


def read_index(outdir) -> List[dict]:
    """读主 index.jsonl。坏行跳过，不抛异常。"""
    index = Path(outdir) / "index.jsonl"
    if not index.exists():
        return []
    records = []
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def run_records(outdir, run_id: str) -> List[dict]:
    return [r for r in read_index(outdir) if r.get("run") == run_id]


def remove_run_from_index(outdir, run_id: str) -> int:
    """把某次会话的记录从主 index.jsonl 里摘掉，返回摘掉的行数。"""
    index = Path(outdir) / "index.jsonl"
    if not index.exists():
        return 0

    kept: List[str] = []
    removed = 0
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)          # 坏行原样留着
            continue
        if record.get("run") == run_id:
            removed += 1
        else:
            kept.append(line)

    index.write_text(
        "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
    )
    return removed


def _sidecar(path: Path) -> Optional[Path]:
    """raw 格式会多一个 .json 边车文件。"""
    side = Path(str(path) + ".json")
    return side if side.exists() else None


def _run_sidecars(outdir, run_id: str) -> List[Path]:
    """这次会话配套的元文件：日志 + 配置。"""
    outdir = Path(outdir)
    candidates = [
        outdir / f"session-{run_id}.log",
        outdir / f"session-{run_id}.json",
    ]
    return [p for p in candidates if p.exists()]


def archive_run(
    outdir, run_id: str, results_root=None
) -> Optional[Path]:
    """把这次会话的帧搬到 results/<run_id>/。返回目标目录；没有帧则返回 None。"""
    outdir = Path(outdir)
    records = run_records(outdir, run_id)
    if not records:
        return None

    root = Path(results_root) if results_root else outdir.parent / "results"
    dest = root / run_id
    dest.mkdir(parents=True, exist_ok=True)

    for record in records:
        source = outdir / record.get("file", "")
        if source.exists():
            shutil.move(str(source), str(dest / source.name))
        side = _sidecar(source)
        if side is not None:
            shutil.move(str(side), str(dest / side.name))

    # 这次会话的日志和配置也一起搬走
    for extra in _run_sidecars(outdir, run_id):
        shutil.move(str(extra), str(dest / extra.name))

    # 归档目录里放一份只含本次会话的 index
    (dest / "index.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    remove_run_from_index(outdir, run_id)
    return dest


def discard_run(outdir, run_id: str) -> int:
    """把这次会话的帧删掉，返回删掉的文件数。

    **走回收站，不是永久删除。** 一次会话可能几十个文件，
    永久删除太危险 —— 用户在资源管理器里按 Delete 都会进回收站。
    回收站失败时抛异常而不是退化成永久删除，宁可不删也不能删错。
    """
    outdir = Path(outdir)
    records = run_records(outdir, run_id)

    targets: List[Path] = []
    for record in records:
        path = outdir / record.get("file", "")
        if path.exists():
            targets.append(path)
        side = _sidecar(path)
        if side is not None:
            targets.append(side)
    targets.extend(_run_sidecars(outdir, run_id))

    if targets and not recycle(targets):
        raise RuntimeError(
            f"有 {len(targets)} 个文件没能送进回收站，已保留原样（不会永久删除）。"
        )

    remove_run_from_index(outdir, run_id)
    return len(targets)


def current_run_id(outdir) -> Optional[str]:
    """最近一次会话的 run_id。"""
    records = read_index(outdir)
    return records[-1].get("run") if records else None
