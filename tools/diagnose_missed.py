"""诊断：为什么有些伤害帧被漏掉了。

把「漏掉」拆成两种可能分别量化:
  A. 帧根本没抓下来   -> 看 index 里的触发方式和时间间隔
  B. 帧抓了但没读出来 -> 用不同的 min_run 跑识别，看差多少

用法:
    python tools/diagnose_missed.py --frames frames2
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbr_recog import damage as dmg  # noqa: E402
from hbr_recog import segment  # noqa: E402


def load_index(frames_dir: Path):
    index = frames_dir / "index.jsonl"
    if not index.exists():
        return []
    out = []
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="诊断漏掉的伤害帧")
    parser.add_argument("--frames", default=str(root / "frames2"))
    parser.add_argument("--templates", default=str(root / "hbr_recog" / "templates.json"))
    args = parser.parse_args(argv)

    frames_dir = Path(args.frames)
    paths = sorted(frames_dir.glob("*.png"))
    if not paths:
        print(f"没有帧: {frames_dir}")
        return 1

    records = load_index(frames_dir)
    by_file = {r["file"]: r for r in records if "file" in r}

    print(f"目录: {frames_dir}")
    print(f"帧数: {len(paths)}   index 记录: {len(records)}")

    # ---------- 抓取侧 ----------
    if records:
        runs = {}
        for r in records:
            runs.setdefault(r.get("run", "?"), []).append(r)
        for run, items in runs.items():
            triggers = Counter(i.get("trigger") for i in items)
            print(f"  run {run}: {len(items)} 帧  触发 {dict(triggers)}")
            peaks = [i.get("peak_diff", 0) for i in items]
            if peaks:
                print(
                    f"      peak_diff: 最小 {min(peaks):.4f}  中位 "
                    f"{sorted(peaks)[len(peaks)//2]:.4f}  最大 {max(peaks):.4f}"
                )
            seqs = [i.get("seq") for i in items]
            if seqs:
                print(f"      seq 范围 {min(seqs)}..{max(seqs)}")

    # ---------- 识别侧：不同 min_run 的差别 ----------
    store = dmg.templates.TemplateStore.load(args.templates)
    readers = {n: dmg.DamageReader(store=store, min_run=n) for n in (1, 2, 3, 4)}

    print("\n=== 不同 min_run 能读出多少伤害数字 ===")
    per_reader = {}
    for n, reader in readers.items():
        found = {}
        for path in paths:
            r = reader.read_one(path)
            if r:
                found[path.name] = r
        per_reader[n] = found
        print(f"  min_run >= {n}: {len(found)} 帧有读数")

    only_strict = set(per_reader[4])
    only_loose = set(per_reader[2])
    missed = sorted(only_loose - only_strict)
    print(
        f"\n被 min_run>=4 过滤掉、但 min_run>=2 能读到的帧: {len(missed)} 个"
    )
    for name in missed:
        r2 = per_reader[2][name]
        r4 = per_reader[4].get(name)
        rec = by_file.get(name, {})
        print(
            f"  {name}  min_run=2 -> {r2.value:>12,} ({r2.digits} 位, 置信 {r2.confidence:.2f})"
            f"   peak_diff={rec.get('peak_diff', '?')}"
        )
        if r4:
            print(f"      （min_run=4 也读到了 {r4.value}）")

    # ---------- 逐帧明细 ----------
    print("\n=== 逐帧明细（min_run=2）===")
    for path in paths:
        rec = by_file.get(path.name, {})
        r = per_reader[2].get(path.name)
        bits = []
        if r:
            bits.append(f"伤害 {r.value:>12,}  ({r.digits} 位, 置信 {r.confidence:.2f})")
        else:
            bits.append("无伤害数字")
        info = []
        if rec:
            info.append(f"[{rec.get('trigger', '?')}]")
            info.append(f"peak={rec.get('peak_diff', '?')}")
        print(f"  {path.name}  {' '.join(info):<34} {' '.join(bits)}")

    # ---------- 用宽松设置再全跑一遍总数 ----------
    print("\n=== 合计对比 ===")
    for n in (1, 2, 3, 4):
        total = sum(r.value for r in per_reader[n].values())
        print(f"  min_run >= {n}: {len(per_reader[n])} 个读数, 合计 {total:,}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
