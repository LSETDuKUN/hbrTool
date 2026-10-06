"""窄长挂件窗口：把抓帧状态和实时预览放在游戏右边，不遮挡游戏。

为什么要有它:
  在命令行里跑 watch 时，那个控制台窗口会压在游戏上，被 screendc 一起抓进画面。
  挂件做窄一点、自动贴到游戏窗口右边的空白处，就完全不挡。

技术要点:
  * 纯 tkinter，不依赖 Pillow。
  * 预览用的是 PPM(P6) 原始数据直接喂给 tk.PhotoImage —— 这是纯标准库里
    唯一能高效刷图的路径（base64 那条路 Tk 不认）。
  * 抓图跑在后台线程，通过 queue 把帧和日志交给主线程刷新，tkinter 不是线程安全的。

复用的是 Monitor 那一整套逻辑（活动性自检、遮挡检测、去重、三种触发、续号），
所以行为跟命令行走 watch 完全一致。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox
from typing import Optional

from . import win32
from .capture import Frame, Grabber
from .monitor import Monitor, MonitorConfig
from .session import archive_run, discard_run, run_records

# ---- 配色
BG = "#191922"
PANEL = "#22222e"
FG = "#e6e6f0"
DIM = "#8b8b9e"
ACCENT = "#ff5c8a"
OK = "#4ade80"
WARN = "#fbbf24"
ERR = "#f87171"


def bgra_to_ppm(frame: Frame, target_width: int) -> tuple:
    """BGRA 帧 -> (PPM P6 字节, 宽, 高)。按最近邻缩放到 target_width。

    tkinter 的 PhotoImage 认原始 PPM 数据，不认 base64，也不要 Pillow。
    1920x1095 缩到 ~292 宽大约 10ms，实时刷新够用。
    """
    step = max(1, int(round(frame.width / max(target_width, 1))))
    width = max(1, frame.width // step)
    height = max(1, frame.height // step)

    view = memoryview(frame.bgra)
    stride = frame.width * 4
    rgb = bytearray(width * height * 3)

    index = 0
    for y in range(height):
        row_base = (y * step) * stride
        for x in range(width):
            offset = row_base + (x * step) * 4
            rgb[index] = view[offset + 2]      # R
            rgb[index + 1] = view[offset + 1]  # G
            rgb[index + 2] = view[offset]      # B
            index += 3

    header = b"P6\n%d %d\n255\n" % (width, height)
    return header + bytes(rgb), width, height


@dataclass
class WidgetConfig:
    match: Optional[str] = None
    hwnd: Optional[int] = None
    outdir: Path = Path("frames")
    #: 默认 280 而不是 300：这块屏上游戏右边只有约 320px 空隙，
    #: 300 宽 + 边框几乎正好占满，留不出余量。
    width: int = 280
    height: int = 960
    fps: float = 15.0
    method: str = "auto"
    hotkey_name: Optional[str] = "F9"
    stop_hotkey_name: Optional[str] = "F10"
    on_change: Optional[float] = 0.02
    settle: float = 0.4
    interval: Optional[float] = None
    min_client: int = 200
    topmost: bool = True
    preview: bool = True
    ask_keep: bool = True      # 会话结束时问一句「这次的帧留着还是删掉」
    #: 认出伤害数字就立刻存。默认开 —— settle 会漏掉绝大部分伤害帧。
    damage_trigger: bool = True
    damage_recheck: int = 1
    #: 同一个数值"连续可见"多久之内算同一次命中（动画长的数字会停留好几秒）
    damage_gap_seconds: float = 2.0
    damage_min_run: int = 1


def compute_placement(
    game_rect,
    phys_w: int,
    phys_h: int,
    screen_w: int,
    screen_h: int,
    margin: int = 8,
    border: int = 32,
):
    """算出挂件该摆在哪个物理像素位置。返回 (x, y, overlaps, spare_right)。

    抽成纯函数是为了能单测 —— 这段逻辑踩过三个坑：
      * 窗口边框不在 Tk 的 geometry 里，不留余量窗口右边缘会伸出屏幕
      * 光看"游戏右边还剩多少"不够，还要保证摆上去之后整体不出屏
      * **游戏窗口最小化时 Windows 返回的矩形是 (-32000, -32000, ...)**，
        照着算会把挂件扔到屏幕外几千像素 —— 表现就是"挂件启动后直接消失"。
        所以最后必须无条件夹回屏幕内。
    """
    left, top, right, bottom = game_rect

    # 最小化的窗口坐标是 -32000 这种哨兵值，没法作为摆放依据
    minimized = right <= -30000 or bottom <= -30000 or left <= -30000

    if minimized:
        # 摆到屏幕右侧，纵向居中
        x = max(0, screen_w - phys_w - border)
        y = max(0, (screen_h - phys_h) // 2)
    else:
        x = right + margin
        if x + phys_w + border > screen_w:
            if left - margin - phys_w - border >= 0:
                x = left - margin - phys_w          # 右边放不下就放左边
            else:
                x = max(0, screen_w - phys_w - border)
        y = max(0, min(top, screen_h - phys_h - border))

    # 无条件夹回屏幕：不管上面算出什么，窗口都必须可见
    x = max(0, min(x, max(0, screen_w - phys_w - border)))
    y = max(0, min(y, max(0, screen_h - phys_h - border)))

    overlaps = not (
        minimized
        or x >= right
        or x + phys_w <= left
        or y >= bottom
        or y + phys_h <= top
    )
    spare = screen_w - right - phys_w - margin
    return x, y, overlaps, spare


class Widget:
    def __init__(self, config: WidgetConfig):
        self.cfg = config
        self.queue: "queue.Queue" = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.monitor: Optional[Monitor] = None
        self._stop = threading.Event()
        self._photo = None
        self._overlap = False
        self._want_running = False
        self._retries = 0
        self._restarts = 0
        self._prompted_run = None
        self._monitor_started_at = 0.0
        self.damage_readings = []
        self.reading_sum = 0
        self.total_damage = 0

        self._build_ui()

    # ------------------------------------------------------------ UI

    def _dpi_factor(self) -> float:
        """DPI-aware Tk geometry already uses physical pixels.

        winfo_fpixels('1i') describes font scaling, not geometry scaling.
        Applying it again moves a 150% DPI window beyond the screen edge.
        """
        return 1.0

    def _build_ui(self) -> None:
        cfg = self.cfg
        win32.set_dpi_aware()
        root = tk.Tk()
        self.root = root
        root.title("HBR 抓帧")
        root.configure(bg=BG)
        # **位置稍后再定**（见 _build_ui 末尾的 _place_beside(None)）：
        # 这里不写位置的话 Tk 会让系统随便挑一个，实测落在屏幕中左（127,70），
        # 而不是用户期望的右侧 —— 而且游戏没找到时 _connect 不会摆位，
        # 窗口就会一直待在系统挑的那个地方。
        root.geometry(
            f"{int(round(cfg.width * self._dpi_factor()))}"
            f"x{int(round(cfg.height * self._dpi_factor()))}"
        )
        root.minsize(240, 420)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if cfg.topmost:
            root.attributes("-topmost", True)

        small = tkfont.Font(family="Microsoft YaHei UI", size=8)
        tiny = tkfont.Font(family="Microsoft YaHei UI", size=7)
        bold = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        self._small, self._bold = small, bold

        # ---- 目标窗口
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=8, pady=(8, 4))
        self.lbl_target = tk.Label(
            head, text="未连接", bg=BG, fg=FG, font=bold, anchor="w", justify="left"
        )
        self.lbl_target.pack(fill="x")
        self.lbl_geom = tk.Label(
            head, text="", bg=BG, fg=DIM, font=tiny, anchor="w", justify="left"
        )
        self.lbl_geom.pack(fill="x")

        # ---- 预览
        self.lbl_preview = tk.Label(root, bg=PANEL, bd=0, highlightthickness=1,
                                    highlightbackground="#33334a")
        self.lbl_preview.pack(fill="x", padx=8, pady=4)

        # ---- 状态
        stats = tk.Frame(root, bg=PANEL)
        stats.pack(fill="x", padx=8, pady=4)
        self._stat_labels = {}
        rows = [
            ("method", "抓图方式"), ("state", "画面"),
            ("saved", "已存帧"), ("damage", "认出伤害"),
            ("skipped", "跳过重复"), ("grabbed", "抓帧次数"),
            ("fps", "实际帧率"), ("diff", "场景变化峰值"),
            ("uptime", "已运行"),
        ]
        for i, (key, label) in enumerate(rows):
            tk.Label(stats, text=label, bg=PANEL, fg=DIM, font=small,
                     anchor="w").grid(row=i, column=0, sticky="w", padx=(6, 4), pady=1)
            value = tk.Label(stats, text="-", bg=PANEL, fg=FG, font=small, anchor="e")
            value.grid(row=i, column=1, sticky="e", padx=(0, 6), pady=1)
            self._stat_labels[key] = value
        stats.columnconfigure(1, weight=1)

        # ---- 遮挡告警
        self.lbl_warn = tk.Label(
            root, text="", bg=PANEL, fg=WARN, font=small,
            wraplength=cfg.width - 28, justify="left", anchor="w",
        )
        self.lbl_warn.pack(fill="x", padx=8, pady=(0, 4))

        # ---- 按钮
        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", padx=8, pady=4)
        self.btn_toggle = tk.Button(
            buttons, text="开始", command=self.toggle, bg=ACCENT, fg="white",
            activebackground="#ff7ba1", relief="flat", font=bold,
        )
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.btn_snap = tk.Button(
            buttons, text="抓一帧", command=self.snap, bg=PANEL, fg=FG,
            activebackground="#2e2e40", relief="flat", font=small,
        )
        self.btn_snap.pack(side="left", fill="x", expand=True, padx=4)
        self.btn_home = tk.Button(
            buttons, text="归位", command=self.reposition, bg=PANEL, fg=FG,
            activebackground="#2e2e40", relief="flat", font=small,
        )
        self.btn_home.pack(side="left", fill="x", expand=True, padx=4)
        self.btn_open = tk.Button(
            buttons, text="打开目录", command=self.open_dir, bg=PANEL, fg=FG,
            activebackground="#2e2e40", relief="flat", font=small,
        )
        self.btn_open.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ---- 实时伤害：每个定稿事件只入账一次，停止后保留。
        self.lbl_damage_sum = tk.Label(root, text="读数累计: 0", bg=PANEL, fg=ACCENT,
            font=bold, anchor="w")
        self.lbl_damage_sum.pack(fill="x", padx=8)
        self.lbl_total_damage = tk.Label(root, text="确认总伤害: 0", bg=PANEL, fg=FG,
            font=small, anchor="w")
        self.lbl_total_damage.pack(fill="x", padx=8)
        tk.Label(root, text="读数累计含平均；确认总伤害仅含合计", bg=BG, fg=DIM,
            font=tiny).pack(fill="x", padx=8)
        damage_wrap = tk.Frame(root, bg=BG)
        damage_wrap.pack(fill="x", padx=8, pady=4)
        self.txt_damage = tk.Text(damage_wrap, height=5, bg=PANEL, fg=FG,
            font=("Consolas", 8), wrap="word", state="disabled")
        damage_scroll = tk.Scrollbar(damage_wrap, command=self.txt_damage.yview)
        self.txt_damage.configure(yscrollcommand=damage_scroll.set)
        damage_scroll.pack(side="right", fill="y")
        self.txt_damage.pack(side="left", fill="both", expand=True)

        # ---- 日志
        log_wrap = tk.Frame(root, bg=BG)
        log_wrap.pack(fill="both", expand=True, padx=8, pady=(4, 2))
        self.txt_log = tk.Text(
            log_wrap, bg=PANEL, fg=DIM, font=("Consolas", 8), bd=0,
            highlightthickness=0, wrap="word", height=6, state="disabled",
        )
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.tag_config("warn", foreground=WARN)
        self.txt_log.tag_config("err", foreground=ERR)
        self.txt_log.tag_config("ok", foreground=OK)

        # ---- 底部提示
        hint = (
            f"[{cfg.hotkey_name}] 抓当前帧"
            if cfg.hotkey_name else ""
        )
        if cfg.stop_hotkey_name:
            hint += f"　[{cfg.stop_hotkey_name}] 停止"
        tk.Label(root, text=hint, bg=BG, fg=DIM, font=small).pack(pady=(0, 8))

        self._log("挂件已就绪。点「开始」连接游戏窗口。")
        self.root.after(80, self._pump)
        self.root.after(2000, self._watchdog)

        # 立刻摆到位 —— 游戏还没开也要贴在右侧。
        # 不主动摆的话窗口会停在系统随便挑的位置（实测屏幕中左），
        # 而游戏没找到时 _connect 一直在重试、不会摆位，就一直待在那儿。
        self._place_beside(None)

        # 窗口是自动识别的，不需要用户给任何参数，直接连
        self.root.after(250, self.start)

    # ------------------------------------------------------------ 线程回调

    def _on_frame_thread(self, frame: Frame, stats: dict) -> None:
        self.queue.put(("frame", frame, stats))

    def _on_log_thread(self, line: str) -> None:
        self.queue.put(("log", line))

    def _on_damage_thread(self, event: dict) -> None:
        self.queue.put(("damage", event))

    def _display_damage(self, event: dict) -> None:
        self.txt_damage.config(state="normal")
        for reading in event["readings"]:
            self.damage_readings.append(reading)
            value = reading["value"]
            label = reading["label"]
            resolved = not reading.get("unresolved", 0)
            if resolved:
                self.reading_sum += value
                if label == "合计":
                    self.total_damage += value
            flag = "（残缺，不累计）" if not resolved else ""
            self.txt_damage.insert("end", f"{len(self.damage_readings):03d} [{label}] "
                f"{reading.get('text', value)} {flag}\n")
        self.txt_damage.see("end")
        self.txt_damage.config(state="disabled")
        self.lbl_damage_sum.config(text=f"读数累计: {self.reading_sum:,}")
        self.lbl_total_damage.config(text=f"确认总伤害: {self.total_damage:,}")

    def _should_stop_thread(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------ 主线程刷新

    def _pump(self) -> None:
        latest = None
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == "log":
                    self._log(item[1])
                elif item[0] == "damage":
                    self._display_damage(item[1])
                elif item[0] == "frame":
                    latest = item  # 只保留最新的一帧，避免刷新落后
        except queue.Empty:
            pass

        if latest is not None:
            _, frame, stats = latest
            self._update_stats(stats)
            if self.cfg.preview:
                self._update_preview(frame)

        if self.thread is not None and not self.thread.is_alive():
            self._on_thread_finished()

        self.root.after(80, self._pump)

    def _update_stats(self, stats: dict) -> None:
        s = self._stat_labels
        s["method"].config(text=str(stats.get("method", "-")))
        broken = stats.get("broken")
        s["state"].config(
            text="接近全黑 ⚠" if broken else "正常",
            fg=ERR if broken else OK,
        )
        s["saved"].config(text=str(stats.get("saved", 0)))
        s["damage"].config(
            text=str(stats.get("damage_hits", 0)),
            fg=ACCENT if stats.get("damage_hits") else FG,
        )
        s["skipped"].config(text=str(stats.get("skipped", 0)))
        s["grabbed"].config(text=str(stats.get("grabbed", 0)))
        elapsed = stats.get("elapsed") or 0
        grabbed = stats.get("grabbed") or 0
        s["fps"].config(text=f"{grabbed / elapsed:.1f}" if elapsed > 0 else "-")
        s["diff"].config(text=f"{stats.get('peak_diff', 0):.4f}")
        s["uptime"].config(text=f"{int(elapsed)}s")

    def _update_preview(self, frame: Frame) -> None:
        try:
            ppm, width, height = bgra_to_ppm(frame, self.cfg.width - 20)
            photo = tk.PhotoImage(data=ppm)
        except Exception as exc:
            self._log(f"预览失败: {type(exc).__name__}: {exc}", "err")
            self.cfg.preview = False
            return
        self._photo = photo
        self.lbl_preview.config(image=photo)

    def _log(self, line: str, tag: str = "") -> None:
        text = self.txt_log
        text.config(state="normal")
        text.insert("end", line + "\n", tag or ())
        # 只留最后 300 行
        if int(text.index("end-1c").split(".")[0]) > 300:
            text.delete("1.0", "100.0")
        text.see("end")
        text.config(state="disabled")

    # ------------------------------------------------------------ 动作

    def toggle(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        """开始。窗口还没开也没关系 —— 会一直重试到游戏出现。"""
        if self.thread is not None and self.thread.is_alive():
            return
        if self._want_running:
            return
        self.damage_readings.clear()
        self.reading_sum = self.total_damage = 0
        self.txt_damage.config(state="normal")
        self.txt_damage.delete("1.0", "end")
        self.txt_damage.config(state="disabled")
        self.lbl_damage_sum.config(text="读数累计: 0")
        self.lbl_total_damage.config(text="确认总伤害: 0")
        self._want_running = True
        self._retries = 0
        self.btn_toggle.config(text="取消", bg="#3a3a4e")
        self._connect()

    def _connect(self) -> None:
        if not self._want_running:
            self._on_thread_finished()
            return

        win32.set_dpi_aware()
        try:
            grabber = Grabber(
                self.cfg.match, hwnd=self.cfg.hwnd, min_client=self.cfg.min_client
            )
            info = grabber.info
        except LookupError as exc:
            self._retries += 1
            if self._retries == 1:
                self._log(f"[等待中] {exc}", "warn")
                self._log("         每 3 秒重试一次。先把游戏开起来即可。")
            elif self._retries % 10 == 0:
                self._log(f"[等待中] 已重试 {self._retries} 次...", "warn")
            self.lbl_target.config(text="等待游戏窗口...", fg=WARN)
            self.lbl_geom.config(text=f"重试 {self._retries} 次")
            self.root.after(3000, self._connect)
            return

        if self._retries:
            self._log(f"连上了（重试了 {self._retries} 次）。", "ok")
        self._retries = 0

        self._place_beside(info)
        self._stop.clear()

        # 顶部标题要更新 —— 之前连着却一直显示"未连接"
        proc = Path(info.process).name if info.process else "?"
        self.lbl_target.config(text=info.title or proc, fg=FG)
        w, h = info.client_size
        self.lbl_geom.config(text=f"{proc}  {w}x{h}  0x{info.hwnd:X}")

        self._log(f"连接: {proc}  {info.title!r}")
        self._log(f"      客户区 {w}x{h} @ {info.client_origin}")

        monitor_cfg = MonitorConfig(
            match=self.cfg.match,
            hwnd=self.cfg.hwnd,
            outdir=Path(self.cfg.outdir),
            fps=self.cfg.fps,
            method=self.cfg.method,
            hotkey_name=self.cfg.hotkey_name,
            stop_hotkey_name=self.cfg.stop_hotkey_name,
            on_change=self.cfg.on_change,
            settle=self.cfg.settle,
            interval=self.cfg.interval,
            min_client=self.cfg.min_client,
            quiet=True,
            minimize_console=False,   # 挂件模式下不要动控制台
            on_frame=self._on_frame_thread,
            on_log=self._on_log_thread,
            on_damage=self._on_damage_thread,
            should_stop=self._should_stop_thread,
            damage_trigger=self.cfg.damage_trigger,
            damage_recheck=self.cfg.damage_recheck,
            damage_gap_seconds=self.cfg.damage_gap_seconds,
            damage_min_run=self.cfg.damage_min_run,
        )
        self.monitor = Monitor(monitor_cfg)
        self.thread = threading.Thread(target=self._run_monitor, daemon=True)
        self.thread.start()
        self._monitor_started_at = time.time()
        self.btn_toggle.config(text="停止", bg="#3a3a4e")

    def _run_monitor(self) -> None:
        try:
            self.monitor.run()
        except Exception as exc:
            self.queue.put(("log", f"[出错] {type(exc).__name__}: {exc}"))

    def stop(self) -> None:
        self._want_running = False
        self._stop.set()
        if self.thread is not None and self.thread.is_alive():
            self._log("已请求停止，等待线程收尾...")
        else:
            # 还在等窗口重试阶段就直接取消了
            self._on_thread_finished()

    def _on_thread_finished(self) -> None:
        self.thread = None

        # 不是用户要停的 —— 自动重连，别让挂件"悄悄死掉"。
        # 之前的写法是直接停住，表现就是"挂件总是丢失"：
        # 游戏窗口最小化一会儿、或者被遮挡触发中止，挂件就再也不干活了。
        if self._want_running:
            # 跑了足够久才算"这次连接是好的"，把退避计数清零
            if self._monitor_started_at and time.time() - self._monitor_started_at > 30:
                self._restarts = 0
            self._restarts += 1
            delay = min(3000 * self._restarts, 30000)
            self._log(
                f"抓图线程结束了（第 {self._restarts} 次），"
                f"{delay // 1000} 秒后自动重连...",
                "warn",
            )
            self.lbl_target.config(text="重连中...", fg=WARN)
            self.root.after(delay, self._connect)
            return

        self.btn_toggle.config(text="开始", bg=ACCENT)
        self._log("已停止。")
        self._maybe_ask_keep()

    # ------------------------------------------------------------ 保存 / 删除

    def _maybe_ask_keep(self) -> None:
        """会话结束后问一次：这次的帧留着还是删掉。

        每次会话有独立的 run_id，所以能准确认出「哪几帧是这一次的」，
        不会误伤之前抓的那些。
        """
        if not self.cfg.ask_keep or self.monitor is None:
            return
        run_id = self.monitor.run_id
        if self._prompted_run == run_id:
            return
        self._prompted_run = run_id

        records = run_records(self.cfg.outdir, run_id)
        if not records:
            return

        total_mb = sum(r.get("bytes", 0) for r in records) / 1024 / 1024
        answer = messagebox.askyesnocancel(
            "保存这次的结果？",
            f"这次抓了 {len(records)} 帧，约 {total_mb:.1f} MB。\n\n"
            f"「是」  → 整理到  results\\{run_id}\\\n"
            f"「否」  → 删掉这次抓的所有帧\n"
            f"「取消」→ 先留在 frames\\ 里，下次再决定",
            parent=self.root,
        )
        if answer is None:
            self._log("保持原样，帧仍在 frames\\ 里。")
            return

        try:
            if answer:
                dest = archive_run(self.cfg.outdir, run_id)
                if dest is None:
                    self._log("没有可归档的帧。", "warn")
                else:
                    self._log(f"已保存到 {dest}", "ok")
                    self._log(f"        {len(records)} 帧 + index.jsonl + session 日志")
            else:
                removed = discard_run(self.cfg.outdir, run_id)
                self._log(f"已删除这次的 {removed} 个文件。")
        except Exception as exc:
            self._log(f"整理失败: {type(exc).__name__}: {exc}", "err")
        finally:
            self.monitor = None

    def _on_close(self) -> None:
        """点右上角关闭：先把抓图停下来，再问一次要不要留。"""
        self._want_running = False
        self._stop.set()
        deadline = time.time() + 2.0
        while (
            self.thread is not None
            and self.thread.is_alive()
            and time.time() < deadline
        ):
            try:
                self.root.update()
            except Exception:
                break
            time.sleep(0.05)
        self._maybe_ask_keep()
        try:
            self.root.destroy()
        except Exception:
            pass

    def snap(self) -> None:
        """手动抓一帧 —— 等同于按热键，但不用切回游戏。"""
        if self.monitor is None:
            self._log("先点「开始」。", "warn")
            return
        try:
            grabber = Grabber(
                self.cfg.match, hwnd=self.cfg.hwnd, min_client=self.cfg.min_client
            )
            frame = grabber.grab()
            self.monitor.cfg.outdir.mkdir(parents=True, exist_ok=True)
            self.monitor._save(frame, "manual", 0.0)
        except Exception as exc:
            self._log(f"抓帧失败: {exc}", "err")

    def open_dir(self) -> None:
        path = Path(self.cfg.outdir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        try:
            import os

            os.startfile(str(path))  # noqa: S606  仅 Windows
        except Exception as exc:
            self._log(f"打开目录失败: {exc}", "err")

    # ------------------------------------------------------------ 摆位

    def _place_beside(self, info) -> None:
        """自动贴到游戏窗口右边的空白处；放不下就退到屏幕最右。

        挂件如果压在游戏上，它自己就会被 screendc 抓进画面，所以这里要算清楚。

        `info` 为 None 表示游戏窗口暂时找不到（没开 / 刚关掉），
        这时按"最小化"分支摆到屏幕右侧居中，保证挂件一定看得见。
        """
        root = self.root
        root.update_idletasks()

        # DPI awareness is set before creating Tk; geometry is in physical pixels.
        factor = self._dpi_factor()

        phys_w = self.cfg.width
        phys_h = self.cfg.height
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()

        # 找不到窗口时用一个"最小化"的哨兵矩形，让 compute_placement 走右侧居中分支
        game_rect = (
            info.rect if info is not None else (-32000, -32000, -31840, -31960)
        )
        margin = 8
        # 窗口边框不在 Tk 的 geometry 里。不留出这点余量的话，
        # 窗口右边缘会伸到屏幕外面去（实测超出 12px）。
        border = 32

        phys_x, phys_y, overlaps, spare = compute_placement(
            game_rect, phys_w, phys_h, screen_w, screen_h, margin, border
        )

        # 再换算成 Tk 单位
        root.geometry(
            f"{int(round(phys_w * factor))}x{int(round(phys_h * factor))}"
            f"+{int(round(phys_x * factor))}+{int(round(phys_y * factor))}"
        )

        self._log(
            f"挂件 {phys_w}x{phys_h} @ +{phys_x}+{phys_y}"
            f"（DPI 换算系数 {factor:.2f}，屏幕 {screen_w}x{screen_h}）"
        )
        try:
            # 诊断用：DPI / 摆位这类问题在 GUI 里不好抄，落一份文件方便排查。
            # **追加**并带时间戳 —— 用户报过"停止后再开始窗口飞了"，
            # 只有记下每次摆位的结果才能看出是哪一次出的问题。
            dbg = Path(self.cfg.outdir).parent / "widget-geometry.log"
            with dbg.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{time.strftime('%H:%M:%S')} "
                    f"game={'None' if info is None else info.rect} "
                    f"factor={factor:.3f} "
                    f"-> phys={phys_w}x{phys_h}@{phys_x}+{phys_y} "
                    f"screen={screen_w}x{screen_h}\n"
                )
        except Exception:
            pass

        self._overlap = overlaps
        if overlaps:
            self.lbl_warn.config(
                text="⚠ 挂件压在游戏上，它会被抓进画面。请手动拖开，或把游戏窗口调小一点。",
                fg=ERR,
            )
        else:
            self.lbl_warn.config(
                text=f"✓ 贴在游戏右侧，不遮挡（右边还剩 {max(spare, 0)}px）",
                fg=OK,
            )

    # ------------------------------------------------------------ 自我看护

    def _physical_bounds(self):
        """Read the real outer window rectangle, including its border."""
        rect = win32.wintypes.RECT()
        hwnd = int(self.root.frame(), 0)
        if not win32.user32.GetWindowRect(hwnd, win32.ctypes.byref(rect)):
            raise win32.ctypes.WinError(win32.ctypes.get_last_error())
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def reposition(self) -> None:
        """把挂件摆回游戏旁边。用户手动点「归位」时走这里。"""
        info = None
        try:
            win32.set_dpi_aware()
            grabber = Grabber(
                self.cfg.match, hwnd=self.cfg.hwnd, min_client=self.cfg.min_client
            )
            info = grabber.info
        except Exception:
            info = None

        self.root.deiconify()
        self.root.lift()
        self._place_beside(info)
        if self.cfg.topmost:
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass

    def _ensure_visible(self) -> bool:
        """确认挂件还在屏幕上、也没压在游戏上。有问题就纠正。返回是否做了纠正。"""
        x, y, width, height = self._physical_bounds()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        # 只要还有一块在屏幕里就算可见
        slack = 60
        visible = (
            x + width > slack
            and x < screen_w - slack
            and y + height > slack
            and y < screen_h - slack
        )
        if not visible:
            self._log(
                f"[看门狗] 挂件跑到屏幕外了（当前 {x},{y} {width}x{height}），拉回来。",
                "warn",
            )
            self.reposition()
            return True

        # 压在游戏上也要挪开 —— 那样挂件会被 screendc 抓进画面里。
        # 只处理"和游戏窗口相交"这一种情况，用户手动拖到别处不会被干涉。
        try:
            win32.set_dpi_aware()
            grabber = Grabber(
                self.cfg.match, hwnd=self.cfg.hwnd, min_client=self.cfg.min_client
            )
            info = grabber.info
        except Exception:
            return False

        if info is None or info.minimized:
            return False

        left, top, right, bottom = info.rect
        overlaps = not (
            x >= right or x + width <= left or y >= bottom or y + height <= top
        )
        if overlaps:
            self._log("[看门狗] 挂件压在游戏上了，挪到旁边。", "warn")
            self._place_beside(info)
            return True
        return False

    def _watchdog(self) -> None:
        """定时自我检查。

        用户报过"停止后再开始窗口不知道飞哪了"。摆位算法已经无条件把坐标夹回屏幕内，
        按理不该发生 —— 与其继续靠推理，不如直接盯着它：跑出去就拉回来。
        自己挪过窗口的话也不会被覆盖（只有真跑到屏幕外才动）。
        """
        try:
            self._ensure_visible()
        except Exception:
            pass
        try:
            self.root.after(2000, self._watchdog)
        except Exception:
            pass

    # ------------------------------------------------------------ 入口

    def run(self) -> None:
        self.root.mainloop()


def run_widget(config: WidgetConfig) -> None:
    """启动挂件。

    **用 pythonw 启动时没有控制台，未捕获的异常会被静默吞掉** ——
    表现就是"双击了但什么都没发生"，完全无从排查。
    所以这里兜住异常：写一份 traceback 到文件，并且弹窗告诉用户。
    """
    try:
        Widget(config).run()
    except Exception:
        tb = traceback.format_exc()
        log_path = None
        try:
            base = Path(config.outdir)
            log_path = base.parent / "widget-error.log"
            log_path.write_text(tb, encoding="utf-8")
        except Exception:
            pass

        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "HBR 挂件启动失败",
                (tb[-1200:] if len(tb) > 1200 else tb)
                + (f"\n\n完整信息已写入:\n{log_path}" if log_path else ""),
            )
            root.destroy()
        except Exception:
            pass
        raise
