"""hbr_capture 命令行。

    python -m hbr_capture list-windows               列出所有可见窗口
    python -m hbr_capture probe   --match 炽焰        试抓一张，报告哪种方法有效
    python -m hbr_capture grab    --match 炽焰 -o a.png
    python -m hbr_capture watch   --match 炽焰 --outdir frames

watch 是实时监视模式：它持续盯着窗口，热键抓当前帧、画面稳定后自动存、
也可以定时存。全程不需要你手动截图，也不需要 alt-tab。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import unicodedata
from pathlib import Path

from . import win32
from .capture import Grabber
from .monitor import Monitor, MonitorConfig


def force_utf8_output() -> None:
    """Windows 控制台默认是 GBK，中文会乱码。统一成 UTF-8。

    同时也改一下控制台代码页，这样在 cmd/PowerShell 里直接跑也是对的。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text)
    )


def _pad(text, width: int, align: str = "left") -> str:
    text = str(text)
    filler = " " * max(0, width - _display_width(text))
    return filler + text if align == "right" else text + filler


def _parse_hwnd(value: str) -> int:
    """接受 0x00150692 或十进制。"""
    try:
        return int(str(value), 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"hwnd 要写成 0x00150692 或十进制整数，收到 {value!r}"
        ) from None


def _pick_window(match, hwnd, min_client: int):
    """定位目标窗口。

    三者优先级: hwnd > match > 都不给（自动识别游戏窗口）。
    自动识别是现在的默认路径，见 win32.find_game_window。
    """
    if hwnd is not None:
        info = win32.describe(hwnd)
        if info is None:
            raise LookupError(f"hwnd 0x{hwnd:08X} 不存在")
        print(f"按 hwnd 钉住窗口: {info}")
        return info

    if not match:
        info = win32.find_game_window(min_client=max(min_client, 400))
        print(f"自动识别到游戏窗口: {info}")
        return info

    hits = win32.find_windows(match, min_client=min_client)
    if not hits:
        raise LookupError(
            f"没找到匹配 {match!r} 的窗口。用 list-windows 看看实际有哪些。"
        )
    if len(hits) == 1:
        print(f"匹配到唯一窗口: {hits[0]}")
        return hits[0]

    hits.sort(key=lambda i: i.client_size[0] * i.client_size[1], reverse=True)
    print(f"匹配 {match!r} 到 {len(hits)} 个窗口，全部列出来：")
    for i, info in enumerate(hits):
        w, h = info.client_size
        proc = Path(info.process).name if info.process else "?"
        mark = "  <-- 默认选这个（客户区最大）" if i == 0 else ""
        print(f"  [{i}] 0x{info.hwnd:08X}  {w}x{h}  {proc}  {info.title!r}{mark}")
    print("  要钉死某个窗口，加 --hwnd 0x......（推荐，避免盯错）")
    print()
    return hits[0]


def _print_windows(windows, show_all: bool) -> None:
    if not windows:
        print("没有找到符合条件的窗口。加 --all 看看全部。")
        return

    headers = ["HWND", "客户区", "进程", "标题"]
    rows = []
    for info in windows:
        w, h = info.client_size
        proc = Path(info.process).name if info.process else "?"
        title = info.title if info.title else "(无标题)"
        if _display_width(title) > 46:
            # 按显示宽度截断，别把中文从中间劈开
            cut = ""
            for ch in title:
                if _display_width(cut + ch) > 43:
                    break
                cut += ch
            title = cut + "..."
        if info.minimized:
            title += " [最小化]"
        rows.append([f"0x{info.hwnd:08X}", f"{w}x{h}", proc, title])

    widths = [
        max(_display_width(headers[i]), *(_display_width(r[i]) for r in rows))
        for i in range(len(headers))
    ]
    aligns = ["right", "right", "left", "left"]

    def line(cells):
        return "  ".join(_pad(c, widths[i], aligns[i]) for i, c in enumerate(cells)).rstrip()

    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row))


def cmd_list_windows(args) -> int:
    # 必须开 DPI 感知，否则报出来的尺寸是被系统缩放过的逻辑坐标
    win32.set_dpi_aware()
    windows = win32.list_windows(
        visible_only=not args.all,
        min_client=args.min_client,
        with_title_only=not args.all,
    )
    windows.sort(key=lambda i: i.client_size[0] * i.client_size[1], reverse=True)
    _print_windows(windows, args.all)
    if args.json:
        payload = [
            {
                "hwnd": hex(i.hwnd),
                "title": i.title,
                "class_name": i.class_name,
                "process": i.process,
                "pid": i.pid,
                "client_size": list(i.client_size),
                "minimized": i.minimized,
            }
            for i in windows
        ]
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写出 {args.json}")
    return 0


def cmd_probe(args) -> int:
    """最关键的一步诊断。

    不只测「能不能抓到」，还测「抓到的是活画面还是冻结帧」——
    后者只看非黑像素比例是看不出来的：冻结帧同样"非全黑"。
    判定办法是每个方法连抓两次，比对是否变化。
    """
    print(f"DPI 感知: {win32.set_dpi_aware()}\n")

    info = _pick_window(args.match, args.hwnd, args.min_client)
    print(f"  窗口矩形   : {info.rect}")
    print(f"  客户区原点 : {info.client_origin}")
    print(f"  标题栏偏移 : {info.title_bar_height}px  （抓图取的是客户区，不影响坐标）")
    print(f"  当前是前台 : {'是' if win32.is_foreground(info.hwnd) else '否'}")
    occluders = [
        o for o in win32.occluding_windows(info.hwnd) if not o["self"] and o["fraction"] >= 0.02
    ]
    if not occluders:
        print("  遮挡检查   : 完整可见 ✅")
    else:
        console = win32.console_window()
        total = sum(o["fraction"] for o in occluders)
        print(f"  遮挡检查   : 被挡住约 {total:.0%}，具体是：")
        for entry in occluders[:4]:
            name = Path(entry["process"]).name if entry["process"] else "?"
            title = entry["title"] or "(无标题)"
            if _display_width(title) > 34:
                title = title[:30] + "..."
            tag = "   <-- 本工具的控制台" if entry["hwnd"] == console else ""
            print(f"               {entry['fraction']:5.1%}  {name}  {title!r}{tag}")
        print("               被挡住的区域会抓到遮挡物，screendc 需要窗口完整可见。")
    if info.minimized:
        print("  !! 窗口是最小化的，请先还原再测。")

    gap = args.gap
    print(f"\n逐个方法试抓 —— 每个连抓两次、间隔 {gap}s，比对是否变化：")
    live: list = []
    usable: list = []
    snapshot = {}

    for method in ("printwindow", "screendc", "bitblt"):
        try:
            started = time.perf_counter()
            first = win32.capture_window(info.hwnd, method)
            elapsed = (time.perf_counter() - started) * 1000
        except Exception as exc:
            print(f"  {method:<12} 失败: {exc}")
            continue

        time.sleep(gap)
        try:
            second = win32.capture_window(info.hwnd, method)
        except Exception as exc:
            print(f"  {method:<12} 第二次抓取失败: {exc}")
            continue

        changed = first.bgra != second.bgra
        if not first.ok:
            verdict = "全黑/花屏"
        elif changed:
            verdict = "活画面 ✅"
            live.append(method)
            usable.append(method)
        else:
            verdict = "两次完全相同 ⚠️"

        if first.ok and method not in usable:
            usable.append(method)
        snapshot.setdefault(method, first)

        print(
            f"  {method:<12} {first.width}x{first.height}  "
            f"非黑像素={first.non_black_ratio:6.1%}  {elapsed:6.1f}ms  -> {verdict}"
        )

    print()
    if live:
        print(f"结论: 用 {' / '.join(live)} 能抓到**活动**的画面。")
        print(f"      推荐: --method {live[0]}")
        if live[0] == "screendc":
            print("      screendc 抓的是屏幕像素，所以游戏必须可见、别被别的窗口盖住。")
        best = snapshot[live[0]]
        if args.save:
            from .capture import Frame

            frame = Frame(
                width=best.width, height=best.height, bgra=best.bgra,
                method=best.method, non_black_ratio=best.non_black_ratio,
            )
            frame.save(args.save, fmt="png")
            print(f"      已经存了一张样例到 {args.save}")
        return 0

    if usable:
        print("结论: 能抓到画面，但**所有方法两次抓取都完全相同**。")
        print("      这有两种可能：")
        print("        - 画面本身是静止的（比如停在菜单里没动）")
        print("        - 或者确实是冻结帧")
        print("      让游戏画面动起来（切个菜单 / 进战斗）再跑一次这个命令，就能区分。")
        if args.save and usable:
            best = snapshot[usable[0]]
            from .capture import Frame

            frame = Frame(
                width=best.width, height=best.height, bgra=best.bgra,
                method=best.method, non_black_ratio=best.non_black_ratio,
            )
            frame.save(args.save, fmt="png")
            print(f"      样例已存到 {args.save}")
        return 3

    print("结论: 三种方法都抓不到有效画面。可能原因:")
    print("  - 游戏在用独占全屏（改成无边框窗口模式再试）")
    print("  - 窗口被最小化")
    print("  - 窗口被别的窗口完全挡住（screendc 会抓不到）")
    return 2


def cmd_grab(args) -> int:
    win32.set_dpi_aware()
    _pick_window(args.match, args.hwnd, args.min_client)
    print()
    grabber = Grabber(
        args.match, hwnd=args.hwnd, method=args.method, min_client=args.min_client
    )
    started = time.perf_counter()
    frame = grabber.grab()
    elapsed = (time.perf_counter() - started) * 1000
    print(f"抓到 {frame.width}x{frame.height} via {frame.method}  "
          f"非黑像素={frame.non_black_ratio:.1%}  {elapsed:.1f}ms")
    size = frame.save(args.output, fmt=args.format, level=args.png_level)
    print(f"已写出 {args.output} ({size:,} 字节)")
    if not frame.ok:
        print("注意: 画面接近全黑。试试 --method bitblt，或者把游戏改成无边框窗口。")
        return 2
    return 0


def cmd_watch(args) -> int:
    win32.set_dpi_aware()
    _pick_window(args.match, args.hwnd, args.min_client)
    print()
    config = MonitorConfig(
        match=args.match,
        hwnd=args.hwnd,
        outdir=Path(args.outdir),
        fps=args.fps,
        method=args.method,
        hotkey_name=None if args.no_hotkey else args.hotkey,
        on_change=None if args.no_auto else args.threshold,
        settle=args.settle,
        interval=args.interval,
        duration=args.duration,
        max_frames=args.max_frames,
        fmt=args.format,
        png_level=args.png_level,
        dedup=not args.no_dedup,
        min_client=args.min_client,
        quiet=args.quiet,
        minimize_console=False if args.keep_console else None,
        stop_hotkey_name=None if args.no_stop_hotkey else args.stop_hotkey,
        damage_trigger=args.damage_trigger,
        damage_recheck=args.damage_recheck,
    )
    monitor = Monitor(config)
    try:
        summary = monitor.run()
    except LookupError as exc:
        print(f"[找不到窗口] {exc}", file=sys.stderr)
        return 1

    if args.ask_keep and summary.get("saved"):
        _ask_keep_cli(config.outdir, monitor.run_id)
    return 0


def _ask_keep_cli(outdir, run_id: str) -> None:
    """会话结束后在控制台问一句：这次抓的帧留着还是删掉。"""
    from .session import archive_run, discard_run, run_records

    records = run_records(outdir, run_id)
    if not records:
        return
    total_mb = sum(r.get("bytes", 0) for r in records) / 1024 / 1024
    print()
    print(f"这次抓了 {len(records)} 帧，约 {total_mb:.1f} MB。")
    print(f"  y = 保存到 results\\{run_id}\\")
    print("  n = 删掉这次抓的所有帧")
    print("  其它 = 先留在 frames\\ 里不动")
    try:
        answer = input("要保存吗？[y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    try:
        if answer == "y":
            dest = archive_run(outdir, run_id)
            print(f"已保存到 {dest}")
        elif answer == "n":
            removed = discard_run(outdir, run_id)
            print(f"已删除 {removed} 个文件。")
        else:
            print("保持原样。")
    except Exception as exc:
        print(f"整理失败: {type(exc).__name__}: {exc}")


def cmd_widget(args) -> int:
    """窄长挂件窗口。"""
    from .widget import WidgetConfig, run_widget

    win32.set_dpi_aware()
    config = WidgetConfig(
        match=args.match,
        hwnd=args.hwnd,
        outdir=Path(args.outdir),
        width=args.width,
        height=args.height,
        fps=args.fps,
        method=args.method,
        hotkey_name=None if args.no_hotkey else args.hotkey,
        stop_hotkey_name=None if args.no_stop_hotkey else args.stop_hotkey,
        on_change=None if args.no_auto else args.threshold,
        settle=args.settle,
        interval=args.interval,
        min_client=args.min_client,
        topmost=not args.no_topmost,
        preview=not args.no_preview,
        ask_keep=not args.no_ask_keep,
        damage_trigger=not args.no_damage_trigger,
        damage_recheck=args.damage_recheck,
    )
    run_widget(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hbr_capture",
        description="HBR 窗口抓帧工具：实时盯着游戏窗口，按需/按事件自动截取",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_target(p):
        p.add_argument("--match", help="窗口标题 / 类名 / 进程名的一部分")
        p.add_argument(
            "--hwnd", type=_parse_hwnd,
            help="直接钉住窗口句柄，如 0x00150692（匹配会撞车时用这个）",
        )

    def add_common(p):
        p.add_argument("--min-client", type=int, default=200,
                       help="客户区最小边长，用来过滤掉小窗口（默认 200）")

    # ---- list-windows
    p = sub.add_parser("list-windows", aliases=["list"],
                       help="列出所有可见窗口，找出游戏窗口")
    p.add_argument("--all", action="store_true", help="不过滤，全部列出来")
    p.add_argument("--json", help="顺便写出 JSON")
    add_common(p)
    p.set_defaults(func=cmd_list_windows)

    # ---- probe
    p = sub.add_parser("probe", help="试抓一张，判断哪种抓图方法在这个游戏上有效")
    add_target(p)
    p.add_argument("--save", help="如果抓成功，顺便存一张样例 PNG")
    p.add_argument("--gap", type=float, default=1.0,
                   help="两次抓取之间的间隔秒数，用来判断活画面/冻结帧（默认 1.0）")
    add_common(p)
    p.set_defaults(func=cmd_probe)

    # ---- grab
    p = sub.add_parser("grab", help="抓一张存下来")
    add_target(p)
    p.add_argument("-o", "--output", default="frame.png")
    p.add_argument("--method", default="auto",
                   choices=["auto", "printwindow", "screendc", "bitblt"])
    p.add_argument("--format", default="png", choices=["png", "raw"])
    p.add_argument("--png-level", type=int, default=1, choices=range(0, 10),
                   metavar="0-9", help="PNG 压缩级别（默认 1）")
    add_common(p)
    p.set_defaults(func=cmd_grab)

    # ---- watch
    p = sub.add_parser("watch", help="实时监视窗口，自动/按热键截帧")
    add_target(p)
    p.add_argument("--outdir", default="frames")
    p.add_argument("--fps", type=float, default=5.0, help="抓帧频率（默认 5）")
    p.add_argument("--method", default="auto",
                   choices=["auto", "printwindow", "screendc", "bitblt"])
    p.add_argument("--hotkey", default="F9", help="抓当前帧的热键（默认 F9）")
    p.add_argument("--no-hotkey", action="store_true", help="关掉热键触发")
    p.add_argument("--threshold", type=float, default=0.02,
                   help="画面变化阈值，越大越不敏感（默认 0.02）")
    p.add_argument("--settle", type=float, default=0.4,
                   help="画面静止多久才算稳定，单位秒（默认 0.4）")
    p.add_argument("--no-auto", action="store_true", help="关掉画面变化自动存")
    p.add_argument("--interval", type=float, default=None,
                   help="每隔 N 秒额外存一张（默认关）")
    p.add_argument("--duration", type=float, default=None,
                   help="跑 N 秒后自动停（默认一直跑）")
    p.add_argument("--max-frames", type=int, default=None, help="最多存多少帧")
    p.add_argument("--format", default="png", choices=["png", "raw"],
                   help="png=体积小；raw=零编码开销，但要占 width*height*4 字节")
    p.add_argument("--png-level", type=int, default=1, choices=range(0, 10),
                   metavar="0-9",
                   help="PNG 压缩级别，越高越小越慢（默认 1，实测 level 1 只比 6 大 3%% 但快 22%%）")
    p.add_argument("--no-dedup", action="store_true",
                   help="关掉去重（默认会跳过和上一张完全相同的帧）")
    p.add_argument("--ask-keep", action="store_true",
                   help="结束时问一句「这次的帧留着还是删掉」")
    p.add_argument("--damage-trigger", action="store_true",
                   help="认出伤害数字就立刻存帧（比 settle 可靠得多）")
    p.add_argument("--damage-recheck", type=int, default=1,
                   help="每 N 帧跑一次伤害识别（默认 1 = 每帧）")
    p.add_argument("--keep-console", action="store_true",
                   help="不要自动最小化本工具的控制台窗口（默认会在它挡住游戏时临时最小化）")
    p.add_argument("--stop-hotkey", default="F10",
                   help="停止热键（默认 F10）")
    p.add_argument("--no-stop-hotkey", action="store_true", help="关掉停止热键")
    p.add_argument("--quiet", action="store_true")
    add_common(p)
    p.set_defaults(func=cmd_watch)

    # ---- widget
    p = sub.add_parser(
        "widget", help="窄长挂件窗口：贴在游戏右边，实时显示抓帧状态和预览"
    )
    add_target(p)
    p.add_argument("--outdir", default="frames")
    p.add_argument("--width", type=int, default=280, help="挂件宽度（默认 280，窄长）")
    p.add_argument("--height", type=int, default=960, help="挂件高度（默认 960）")
    p.add_argument("--fps", type=float, default=15.0)
    p.add_argument("--method", default="auto",
                   choices=["auto", "printwindow", "screendc", "bitblt"])
    p.add_argument("--hotkey", default="F9", help="抓当前帧热键（默认 F9）")
    p.add_argument("--no-hotkey", action="store_true")
    p.add_argument("--stop-hotkey", default="F10", help="停止热键（默认 F10）")
    p.add_argument("--no-stop-hotkey", action="store_true")
    p.add_argument("--threshold", type=float, default=0.02, help="画面变化阈值")
    p.add_argument("--no-auto", action="store_true", help="关掉画面变化自动存")
    p.add_argument("--settle", type=float, default=0.4)
    p.add_argument("--interval", type=float, default=None)
    p.add_argument("--no-topmost", action="store_true", help="不要总在最前")
    p.add_argument("--no-preview", action="store_true", help="关掉实时预览图")
    p.add_argument("--no-ask-keep", action="store_true",
                   help="停止时不要问「这次的帧留着还是删掉」")
    p.add_argument("--no-damage-trigger", action="store_true",
                   help="关掉内容触发（默认会在认出伤害数字时立刻存帧）")
    p.add_argument("--damage-recheck", type=int, default=1,
                   help="每 N 帧跑一次伤害识别（默认 1 = 每帧，约 4ms）")
    add_common(p)
    p.set_defaults(func=cmd_widget)

    return parser


def main(argv=None) -> int:
    force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    # 目标窗口是可选的: 不给就自动识别游戏窗口（见 win32.find_game_window）。
    # 这里以前强制要求 --match/--hwnd，结果去掉了 bat 里的 --match 之后
    # 挂件直接启动不了 —— 而且 pythonw 没有控制台，错误信息根本看不见。
    try:
        return args.func(args)
    except LookupError as exc:
        # 窗口找不到是使用中最常见的错误。走 stdout 而不是 stderr:
        # PowerShell 会把原生程序的 stderr 渲染成红色 NativeCommandError，
        # 看起来像崩溃，而这只是个预期内的用户错误。退出码仍然非 0。
        print(f"[找不到窗口] {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"[路径错误] {exc}")
        return 1
    except RuntimeError as exc:
        # 抓图彻底失败（三种方法都不行），这里才是给建议的地方
        print(f"[抓图失败] {exc}")
        print(
            "  排查顺序:\n"
            "    1. 让游戏窗口保持可见 —— screendc 抓的是屏幕像素，被盖住就会抓到遮挡物\n"
            "    2. 窗口别最小化\n"
            "    3. 把游戏改成「无边框窗口」模式，独占全屏经常抓不到\n"
            "    4. 跑 probe 看三种方法各自的结果（它会标出哪个是活画面）"
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
