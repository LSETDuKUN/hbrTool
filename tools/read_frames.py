"""批量读取一批帧里的伤害数字，按回合列出并求和。

用法:
    # 直接看每一帧读到多少
    python tools/read_frames.py

    # 按回合分组（按帧顺序，一个逗号一个回合号）
    python tools/read_frames.py --turns 1,1,1,2,2,3

    # 导出成 hbr_calc 能吃的战斗 JSON
    python tools/read_frames.py --turns 1,1,2 --emit-battle battle.json \
        --boss-name 阿蒙之门Ω --boss-dp 5000000 --boss-hp 20000000

    # 结构化输出
    python tools/read_frames.py --json readings.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbr_recog import damage as dmg  # noqa: E402


def load_timestamps(frames_dir: Path) -> dict:
    """从 index.jsonl 里取每帧的抓取时间，没有就算了。"""
    index = frames_dir / "index.jsonl"
    if not index.exists():
        return {}
    out = {}
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "file" in record and "time" in record:
            out[record["file"]] = record["time"]
    return out


def fmt(value: float) -> str:
    return f"{value:,.0f}"


def superseded_damage_files(frames_dir: Path, paths) -> set:
    """Only explicit event revisions supersede older evidence, never equal values."""
    from hbr_capture.session import read_index
    available = {p.name for p in paths}
    groups, superseded = {}, set()
    for record in read_index(frames_dir):
        name, identity = record.get('file'), record.get('damage_event_id')
        run = record.get('run')
        if (not isinstance(name, str) or name not in available
                or not isinstance(identity, (int, str)) or not isinstance(run, str)
                or record.get('trigger') not in ('damage', 'damage_update')):
            continue
        key = (run, identity)
        previous = groups.setdefault(key, [])
        if record.get('trigger') == 'damage_update':
            superseded.update(p for p in previous if p != name)
        previous.append(name)
    return superseded


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="批量读取帧里的伤害数字")
    parser.add_argument("--frames", default=str(root / "frames"))
    parser.add_argument("--templates", default=str(root / "hbr_recog" / "templates.json"))
    parser.add_argument("--turns", help="按帧顺序给出回合号，如 1,1,2,2,3")
    parser.add_argument("--min-score", type=float, default=0.45,
                        help="单字置信度低于此值算没认出来（默认 0.45）")
    parser.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    parser.add_argument("--emit-battle", help="导出 hbr_calc 可读的战斗 JSON")
    parser.add_argument("--boss-name", default="BOSS")
    parser.add_argument("--boss-dp", type=float, default=0.0)
    parser.add_argument("--boss-hp", type=float, default=0.0)
    parser.add_argument("--break-rate", type=float, default=1.0)
    args = parser.parse_args(argv)

    frames_dir = Path(args.frames)
    paths = sorted(frames_dir.glob("*.png"))
    if not paths:
        print(f"没找到帧: {frames_dir}")
        return 1

    # min_run=1 和挂件保持一致 —— 不然复核时会看到"漏"，其实是两边参数不同：
    # 挂件用 min_run=1 所以存下了 3 位甚至 1 位的伤害帧，而这里用默认的 4 会读不出来。
    reader = dmg.DamageReader(
        store=dmg.templates.TemplateStore.load(args.templates),
        min_score=args.min_score,
        min_run=1,
        repair_unknown=True,
    )
    times = load_timestamps(frames_dir)
    superseded = superseded_damage_files(frames_dir, paths)

    turn_list = None
    if args.turns:
        try:
            turn_list = [int(x) for x in args.turns.replace(" ", "").split(",") if x]
        except ValueError:
            print("--turns 必须是逗号分隔的整数，如 1,1,2,2", file=sys.stderr)
            return 1
        if len(turn_list) != len(paths):
            print(
                f"--turns 给了 {len(turn_list)} 个，但有 {len(paths)} 帧，对不上。",
                file=sys.stderr,
            )
            return 1

    readings = []
    for i, path in enumerate(paths):
        # Keep the original frame/turn slots; replaced evidence adds no hit.
        found = [] if path.name in superseded else reader.read_file(path)
        readings.append(
            {
                "file": path.name,
                "superseded": path.name in superseded,
                "time": times.get(path.name, ""),
                "turn": turn_list[i] if turn_list else None,
                "hits": [
                    {
                        "value": r.value,
                        "text": r.text,
                        "label": r.label,
                        "label_score": round(r.label_score, 3),
                        "is_total": r.is_total,
                        "confidence": round(r.confidence, 3),
                        "box": list(r.box),
                        "unresolved": r.unresolved,
                    }
                    for r in found
                ],
            }
        )

    # ---------------- 打印 ----------------
    by_turn = {}
    for item in readings:
        by_turn.setdefault(item["turn"], []).append(item)

    total_sum = 0.0        # 只有标签明确是「合计」的才计入
    average_sum = 0.0      # 平均值单独列，不混进总和
    unknown_count = 0

    for turn in sorted(by_turn, key=lambda t: (t is None, t)):
        label = f"回合 {turn}" if turn is not None else "全部帧"
        print(f"\n=== {label} ===")
        subtotal = 0.0
        for item in by_turn[turn]:
            if not item["hits"]:
                continue
            for hit in item["hits"]:
                tag = hit["label"]
                if tag == "合计" and not hit["unresolved"]:
                    subtotal += hit["value"]
                elif tag == "平均":
                    average_sum += hit["value"]
                else:
                    unknown_count += 1
                flag = ""
                if hit["unresolved"]:
                    flag += f"  [{hit['unresolved']} 位没认出来]"
                if hit["confidence"] < 0.6:
                    flag += "  <-- 数字置信偏低"
                if tag == "未知":
                    flag += "  <-- 标签没认出来，不计入总和"
                print(
                    f"  {item['file']}  {item['time']}  [{tag}伤害] "
                    f"{hit['text']:>12} = {fmt(hit['value']):>14}"
                    f"  (数字 {hit['confidence']:.2f} / 标签 {hit['label_score']:.2f}){flag}"
                )
        print(f"  小计（仅合计伤害）: {fmt(subtotal)}")
        total_sum += subtotal

    print(f"\n合计（仅「合计伤害」标签的读数）: {fmt(total_sum)}")
    if average_sum:
        print(
            f"另有「平均伤害」读数合计 {fmt(average_sum)} —— "
            "**没有计入**，因为平均值不能当总和用"
        )
    if unknown_count:
        print(f"另有 {unknown_count} 条标签没认出来，也没计入")
    print(f"（共 {len(paths)} 帧，{sum(len(r['hits']) for r in readings)} 个伤害数字）")

    # ---------------- 导出 ----------------
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"frames": readings, "total": total_sum}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        print(f"已写出 {args.json_out}")

    if args.emit_battle:
        # 每一帧读到的「合计伤害」当成一个动作。
        # 注意: 这里只能给出动作**总和**，不是逐 hit 的数值 ——
        # 因为单 hit 只作用于一个池子，用总和去推 Boss DP/HP 会偏乐观
        # （真实情况里前几 hit 可能都打在盾上、白打很多）。
        turns = []
        for turn in sorted(by_turn, key=lambda t: (t is None, t)):
            actions = []
            for item in by_turn[turn]:
                for hit in item["hits"]:
                    if hit["label"] != "合计" or hit["unresolved"]:
                        continue
                    actions.append(
                        {
                            "character": item["file"],
                            "kind": "skill",
                            "skill": f"读取自 {item['file']}",
                            "hits": 1,
                            "damage": hit["value"],
                        }
                    )
            if actions:
                turns.append({"turn": turn if turn is not None else 1, "actions": actions})

        battle = {
            "boss": {
                "name": args.boss_name,
                "dp_max": args.boss_dp,
                "hp_max": args.boss_hp,
                "break_rate": args.break_rate,
            },
            "turns": turns,
        }
        Path(args.emit_battle).write_text(
            json.dumps(battle, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写出战斗 JSON: {args.emit_battle}")
        print("  注意: 每帧的「合计伤害」被当成 1 个 hit。")
        print("        真实情况是多段命中，因为「单 hit 只作用于一个池子」，")
        print("        用总和推 Boss DP/HP 会偏乐观。要精确需要逐 hit 的数值。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
