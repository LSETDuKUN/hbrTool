"""命令行入口。

用法:
    python -m hbr_calc.cli examples/battle_synthetic.json
    python -m hbr_calc.cli examples/battle_synthetic.json --json out.json
    python -m hbr_calc.cli examples/battle_synthetic.json --csv turns.csv
    python -m hbr_calc.cli --demo
    python -m hbr_calc.cli examples/battle_screen.json --od-per-level 120

退出码: 0 成功, 1 输入或规则错误, 2 用法错误（argparse 自带）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .battle import load_spec, run_battle
from .od import OdRuleError
from .report import render_text, report_to_dict, report_to_rows

EXIT_OK = 0
EXIT_INPUT_ERROR = 1

DEMO_SPEC = {
    "boss": {
        "name": "示例Boss",
        "dp_max": 10000,
        "hp_max": 10000,
        "break_rate": 2.0,
    },
    "od": {"od_per_level": 100.0, "max_level": 3},
    "turns": [
        {
            "turn": 1,
            "observed_od_percent": 35.875,
            "actions": [
                {
                    "character": "月城最中",
                    "kind": "skill",
                    "skill": "得意洋洋",
                    "hits": 5,
                    "combo_orbs": 2,
                    "chain_bonus": 0.15,
                    "damage": 3000,
                },
                {
                    "character": "佐月",
                    "kind": "skill",
                    "skill": "连击",
                    "hits": 3,
                    "combo_orbs": 0,
                    "chain_bonus": 0.10,
                    "damage": 2000,
                },
                {
                    "character": "月城最中",
                    "kind": "normal",
                    "damage": 1500,
                },
            ],
        },
        {
            "turn": 2,
            "observed_od_percent": 50.25,
            "actions": [
                {
                    "character": "月城最中",
                    "kind": "skill",
                    "skill": "充能注入",
                    "hits": 4,
                    "combo_orbs": 1,
                    "chain_bonus": 0.15,
                    "damage": 4000,
                }
            ],
        },
    ],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hbr_calc",
        description="HBR 战斗数值推演: 逐回合伤害统计 + Boss DP/HP + 超频条计算",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="战斗输入 JSON 路径（用 --demo 时可不填）",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="跑内置的示例数据，不需要输入文件",
    )
    parser.add_argument("--json", dest="json_out", help="把结构化结果写到这个路径")
    parser.add_argument("--csv", dest="csv_out", help="把逐回合汇总写成 CSV")
    parser.add_argument(
        "--no-detail",
        action="store_true",
        help="不打印逐动作明细",
    )
    parser.add_argument(
        "--od-per-level",
        type=float,
        default=None,
        help="覆盖『多少百分比 = 1 级 OD』（默认 100，待校准）",
    )
    parser.add_argument(
        "--max-level",
        type=int,
        default=None,
        help="覆盖 OD 等级上限（默认 3）",
    )
    return parser


def _write_csv(path: Path, rows) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fieldnames = list(rows[0].keys())
    # utf-8-sig 让 Excel 直接正确识别中文表头
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.demo:
        spec_source = DEMO_SPEC
    elif args.input:
        spec_source = args.input
    else:
        parser.error("需要给一个输入 JSON 路径，或者加 --demo")

    try:
        spec = load_spec(spec_source)

        # 命令行覆盖
        overrides = {}
        if args.od_per_level is not None:
            overrides["od_per_level"] = args.od_per_level
        if args.max_level is not None:
            overrides["max_level"] = args.max_level
        if overrides:
            from dataclasses import replace

            spec.od_config = replace(spec.od_config, **overrides)

        report = run_battle(spec)
    except OdRuleError as exc:
        print(f"[规则错误] {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except (ValueError, FileNotFoundError) as exc:
        print(f"[输入错误] {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    print(render_text(report, detail=not args.no_detail))

    if args.json_out:
        path = Path(args.json_out)
        path.write_text(
            json.dumps(report_to_dict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已写出 JSON: {path}")

    if args.csv_out:
        path = Path(args.csv_out)
        _write_csv(path, report_to_rows(report))
        print(f"已写出 CSV : {path}")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
