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
import json
import threading
import time
import tkinter as tk
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox
from typing import Optional

from . import win32
from .capture import Frame, Grabber
from .monitor import Monitor, MonitorConfig
from .session import archive_run, discard_run, run_records
from .damage_stats import DamageLedger, enemy_count, pool_value, resource_state
from .widget_ui import ResourceMeter

# ---- 配色
BG = "#121724"
PANEL = "#1d2333"
FG = "#e6e6f0"
DIM = "#8b8b9e"
ACCENT = "#ff8d86"
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
    #: 留出窗口边框余量，适配窗口模式下游戏右侧的窄空隙。
    width: int = 250
    height: int = 760
    fps: float = 15.0
    method: str = "auto"
    hotkey_name: Optional[str] = "F9"
    stop_hotkey_name: Optional[str] = "F10"
    on_change: Optional[float] = 0.02
    settle: float = 0.4
    interval: Optional[float] = None
    min_client: int = 200
    topmost: bool = True
    preview: bool = False
    enemy_count: int = 1
    dp_max: int = 0
    hp_max: int = 0
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
                x = left - margin - phys_w - border  # Include the outer frame on the left.
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


def compute_panel_width(game_rect, preferred, screen_w, minimum=220):
    """Fit the client width into the larger side gap, reserving border/margin."""
    if game_rect[0] <= -30000:
        return preferred
    room = max(game_rect[0], screen_w - game_rect[2]) - 40
    return min(preferred, room) if room >= minimum else preferred


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
        self.ledger = DamageLedger()
        self.ledger.set_enemies(config.enemy_count)
        self.cfg.dp_max = pool_value(config.dp_max, "DP")
        self.cfg.hp_max = pool_value(config.hp_max, "HP")
        self._settings_dialog = None

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
        root = self.root = tk.Tk()
        root.title("HBR 伤害")
        root.configure(bg=BG)
        root.geometry(f"{cfg.width}x{cfg.height}")
        root.minsize(220, 640)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.attributes("-topmost", cfg.topmost)
        small = tkfont.Font(family="Microsoft YaHei UI", size=8)
        tiny = tkfont.Font(family="Microsoft YaHei UI", size=7)
        bold = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        self._small, self._bold = small, bold

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=12, pady=(12, 6))
        self.lbl_target = tk.Label(head, text="等待游戏", bg=BG, fg=DIM,
                                   font=small, anchor="w")
        self.lbl_target.pack(side="left", fill="x", expand=True)
        menu_button = tk.Menubutton(head, text="⋯", bg=PANEL, fg=FG, relief="flat",
                                    font=bold, padx=8)
        menu_button.pack(side="right")
        menu = tk.Menu(menu_button, tearoff=False, bg=PANEL, fg=FG,
                       activebackground=ACCENT, activeforeground="white")
        menu_button.configure(menu=menu)
        menu.add_command(label="战斗设置 · DP / HP / 怪数", command=self._open_battle_settings)
        menu.add_separator()
        menu.add_command(label="抓取当前帧  F9", command=self.snap)
        menu.add_command(label="窗口归位", command=self.reposition)
        menu.add_command(label="打开帧目录", command=self.open_dir)
        menu.add_separator()
        self._preview_var = tk.BooleanVar(value=cfg.preview)
        menu.add_checkbutton(label="画面预览", variable=self._preview_var,
                             command=self._toggle_preview)
        self._topmost_var = tk.BooleanVar(value=cfg.topmost)
        menu.add_checkbutton(label="保持置顶", variable=self._topmost_var,
                             command=self._toggle_topmost)
        menu.add_command(label="日志与诊断", command=self._toggle_details)

        card = tk.Frame(root, bg=PANEL, padx=10, pady=8, highlightthickness=1,
                        highlightbackground="#34374f")
        card.pack(fill="x", padx=12, pady=6)
        tk.Label(card, text="累计伤害", bg=PANEL, fg=DIM, font=small,
                 anchor="w").pack(fill="x")
        self._total_font = tkfont.Font(family="Segoe UI", size=18, weight="bold", slant="italic")
        self.lbl_damage_sum = tk.Label(card, text="0", bg=PANEL, fg=ACCENT,
            font=self._total_font, anchor="w")
        self.lbl_damage_sum.pack(fill="x")
        self.lbl_total_damage = tk.Label(card, text="已确认 0 笔 · 待确认 0 笔",
                                         bg=PANEL, fg=DIM, font=tiny, anchor="w")
        self.lbl_total_damage.pack(fill="x", pady=(3, 0))
        self.lbl_last_damage = tk.Label(card, text="最近有效伤害  —", bg=PANEL,
            fg="#ffca9d", font=("Microsoft YaHei UI", 8, "bold", "italic"), anchor="w")
        self.lbl_last_damage.pack(fill="x", pady=(7, 0))

        resources = tk.Frame(root, bg=BG)
        resources.pack(fill="x", padx=12, pady=(3, 5))
        self.dp_meter = ResourceMeter(resources, "DP", "护盾", ("#4568ee", "#6ae1ff"))
        self.dp_meter.pack(fill="x", pady=(0, 6))
        self.hp_meter = ResourceMeter(resources, "HP", "生命", ("#ea587c", "#ffb07d"))
        self.hp_meter.pack(fill="x")

        targets = tk.Frame(root, bg=BG)
        targets.pack(fill="x", padx=12, pady=(3, 8))
        self.lbl_pool_hint = tk.Label(targets, text="", bg=BG, fg=DIM, font=tiny, anchor="w")
        self.lbl_pool_hint.pack(side="left", fill="x", expand=True)
        tk.Button(targets, text="战斗设置", command=self._open_battle_settings, bg=PANEL,
            fg="#8ee8ff", relief="flat", font=small, padx=6).pack(side="right")
        self._enemy_var = tk.StringVar(value=str(self.ledger.enemies))
        self.lbl_enemy_hint = tk.Label(root, text="平均 × 怪数；修改后重算本轮",
            bg=BG, fg=DIM, font=tiny, anchor="w")

        self.btn_toggle = tk.Button(root, text="开始", command=self.toggle, bg=ACCENT,
            fg="white", activebackground="#ff7ba1", relief="flat", font=bold, pady=5)
        self.btn_toggle.pack(fill="x", padx=12, pady=(0, 8))
        self.lbl_preview = tk.Label(root, bg=PANEL, bd=0)
        self._content = tk.Frame(root, bg=BG)
        self._content.pack(fill="both", expand=True, padx=12)
        tk.Label(self._content, text="伤害明细", bg=BG, fg=FG, font=bold,
                 anchor="w").pack(fill="x", pady=(0, 5))
        tk.Label(self._content, text="未知 / 残缺不入账", bg=BG, fg=DIM,
                 font=tiny, anchor="w").pack(fill="x", pady=(0, 5))
        damage_wrap = tk.Frame(self._content, bg=PANEL)
        damage_wrap.pack(fill="both", expand=True)
        self.txt_damage = tk.Text(damage_wrap, height=4, bg=PANEL, fg=FG,
            font=("Microsoft YaHei UI", 8), wrap="word", state="disabled",
            relief="flat", highlightthickness=0, padx=6, pady=6)
        scroll = tk.Scrollbar(damage_wrap, command=self.txt_damage.yview)
        self.txt_damage.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_damage.pack(side="left", fill="both", expand=True)
        self.txt_damage.tag_config("pending", foreground=WARN)
        self.txt_damage.tag_config("confirmed", foreground=FG)

        self._details_window = tk.Toplevel(root)
        self._details_window.withdraw()
        self._details_window.title("HBR · 日志与诊断")
        self._details_window.configure(bg=BG)
        self._details_window.geometry("460x420")
        self._details_window.minsize(360, 280)
        self._details_window.transient(root)
        self._details_window.protocol("WM_DELETE_WINDOW", self._hide_details)
        self._details = tk.Frame(self._details_window, bg=BG)
        self._details.pack(fill="both", expand=True, padx=12, pady=12)
        self._details_visible = False
        self.lbl_geom = tk.Label(self._details, text="", bg=BG, fg=DIM, font=tiny)
        self.lbl_geom.pack(fill="x")
        stats = tk.Frame(self._details, bg=PANEL)
        stats.pack(fill="x", pady=3)
        self._stat_labels = {}
        rows = [("method", "抓图方式"), ("state", "画面"),
                ("saved", "已存帧"), ("damage", "伤害事件"),
                ("skipped", "跳过重复"), ("grabbed", "抓帧次数"),
                ("fps", "实际帧率"), ("diff", "变化峰值"), ("uptime", "已运行")]
        for i, (key, label) in enumerate(rows):
            row, col = divmod(i, 2)
            tk.Label(stats, text=label, bg=PANEL, fg=DIM, font=tiny).grid(
                row=row, column=col * 2, sticky="w", padx=3)
            value = tk.Label(stats, text="-", bg=PANEL, fg=FG, font=tiny)
            value.grid(row=row, column=col * 2 + 1, sticky="e", padx=3)
            self._stat_labels[key] = value
        stats.columnconfigure(1, weight=1)
        stats.columnconfigure(3, weight=1)
        self.txt_log = tk.Text(self._details, bg=PANEL, fg=DIM, font=("Consolas", 7),
            relief="flat", highlightthickness=0, wrap="word", height=4, state="disabled")
        self.txt_log.pack(fill="both", expand=True, pady=3)
        for tag, color in (("warn", WARN), ("err", ERR), ("ok", OK)):
            self.txt_log.tag_config(tag, foreground=color)

        self.btn_details = tk.Button(root, text="日志与诊断 ↗", bg=BG, fg=DIM,
            relief="flat", font=tiny, command=self._toggle_details, anchor="w")
        self.btn_details.pack(fill="x", padx=12, pady=(6, 2))
        self.lbl_warn = tk.Label(root, text="", bg=BG, fg=WARN, font=tiny,
                                wraplength=cfg.width - 24, justify="left", anchor="w")
        self.lbl_warn.pack(fill="x", padx=12, pady=(0, 8))
        self._render_resources()
        self._toggle_preview()
        self._log("挂件已就绪；未知和残缺读数不计入累计伤害。")
        self.root.after(80, self._pump)
        self.root.after(2000, self._watchdog)
        self._place_beside(None)
        self.root.after(250, self.start)

    def _toggle_details(self):
        if self._details_visible:
            self._hide_details()
        else:
            self._details_visible = True
            self._place_auxiliary(self._details_window, 460, 420)
            self._details_window.deiconify()
            self._details_window.lift()

    def _hide_details(self):
        self._details_visible = False
        self._details_window.withdraw()

    def _desired_height(self):
        height = self.cfg.height + (140 if self.cfg.preview else 0)
        return min(height, self.root.winfo_screenheight() - 80)

    def _resize_panel(self):
        self.root.update_idletasks()
        x, y, _, _ = self._physical_bounds()
        width, height = self.root.winfo_width(), self._desired_height()
        if not self._want_running:
            self.root.geometry(f"{width}x{height}")
            return
        x = max(0, min(x, self.root.winfo_screenwidth() - width - 32))
        y = max(0, min(y, self.root.winfo_screenheight() - height - 60))
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _toggle_preview(self):
        self.cfg.preview = self._preview_var.get()
        if self.cfg.preview:
            self.lbl_preview.pack(fill="x", padx=12, pady=(0, 6), before=self._content)
        else:
            self.lbl_preview.pack_forget()
        if hasattr(self, "lbl_warn"):
            self._resize_panel()

    def _toggle_topmost(self):
        self.cfg.topmost = self._topmost_var.get()
        self.root.attributes("-topmost", self.cfg.topmost)

    def _change_enemies(self, event=None):
        try:
            self.ledger.set_enemies(self._enemy_var.get())
        except ValueError as exc:
            self.lbl_enemy_hint.config(text=str(exc), fg=ERR)
            return
        self.cfg.enemy_count = self.ledger.enemies
        self.lbl_enemy_hint.config(text="平均 × 怪数；修改后重算本轮", fg=DIM)
        self._render_damage()
        self._save_totals({"type": "enemy_count", "enemy_count": self.ledger.enemies})

    def _open_battle_settings(self):
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            self._settings_dialog.lift()
            return
        dialog = self._settings_dialog = tk.Toplevel(self.root)
        dialog.title("HBR · 战斗设置")
        dialog.configure(bg=BG)
        self._place_auxiliary(dialog, 380, 420)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        body = tk.Frame(dialog, bg=BG, padx=20, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="设置本轮初始值", font=self._bold, bg=BG, fg=FG,
                 anchor="w").pack(fill="x", pady=(0, 12))
        values = {}
        for key, title, value, color in (("dp", "初始 DP", self.cfg.dp_max, "#6ae1ff"),
                ("hp", "初始 HP", self.cfg.hp_max, "#ffb07d"),
                ("enemies", "怪物数量", self.ledger.enemies, FG)):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=title, font=self._small, bg=BG, fg=color, width=9,
                     anchor="w").pack(side="left")
            variable = tk.StringVar(value=f"{value:,}")
            values[key] = variable
            entry = tk.Entry(row, textvariable=variable, font=self._small, bg=PANEL,
                fg=FG, insertbackground=FG, relief="flat", justify="right")
            entry.pack(side="right", fill="x", expand=True, ipady=5)
            if key == "dp":
                entry.focus_set()
                entry.selection_range(0, "end")
        tk.Label(body, text="伤害先扣 DP，超出部分扣 HP。\n修改后重算本轮；新一轮恢复初始值。",
            bg=BG, fg=DIM, font=self._small, justify="left", anchor="w").pack(fill="x", pady=(10, 2))
        error = tk.Label(body, text="", bg=BG, fg=ERR, font=self._small, anchor="w")
        error.pack(fill="x", pady=4)

        def apply_values():
            try:
                self._set_battle_limits(values["dp"].get(), values["hp"].get(), values["enemies"].get())
            except ValueError as exc:
                error.config(text=str(exc))
                return
            dialog.destroy()
            self._settings_dialog = None

        tk.Button(body, text="应用并重算", command=apply_values, bg=ACCENT, fg=BG,
            relief="flat", font=self._bold, pady=7).pack(fill="x")
        dialog.bind("<Return>", lambda event: apply_values())
        dialog.bind("<Escape>", lambda event: dialog.destroy())

    def _place_auxiliary(self, window, width, height):
        self.root.update_idletasks()
        x, y, panel_width, _ = self._physical_bounds()
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        right = x + panel_width + 8
        x = right if right + width + 32 <= screen_width else x - width - 32
        x = max(0, min(x, screen_width - width - 32))
        y = max(0, min(y, screen_height - height - 60))
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _set_battle_limits(self, dp, hp, enemies):
        dp, hp, enemies = pool_value(dp, "DP"), pool_value(hp, "HP"), enemy_count(enemies)
        self.cfg.dp_max, self.cfg.hp_max, self.cfg.enemy_count = dp, hp, enemies
        self.ledger.set_enemies(enemies)
        self._enemy_var.set(str(enemies))
        self._render_damage()
        self._save_totals({"type": "battle_settings"})

    def _render_resources(self):
        state = resource_state(self.cfg.dp_max, self.cfg.hp_max,
                               self.ledger.total, self.ledger.last_damage)
        self.dp_meter.set_values(state.dp, state.dp_max, state.last_dp_loss)
        self.hp_meter.set_values(state.hp, state.hp_max, state.last_hp_loss)
        text = f"最近有效伤害  {state.last_damage:,}" if state.last_damage else "最近有效伤害  —"
        self.lbl_last_damage.config(text=text)
        self.lbl_pool_hint.config(text=f"{self.ledger.enemies} 怪 · DP → HP"
            if state.dp_max or state.hp_max else "请设置初始值 →")

    # ------------------------------------------------------------ 线程回调

    def _on_frame_thread(self, frame: Frame, stats: dict) -> None:
        self.queue.put(("frame", frame, stats))

    def _on_log_thread(self, line: str) -> None:
        self.queue.put(("log", line))

    def _on_damage_thread(self, event: dict) -> None:
        self.queue.put(("damage", event))

    def _display_damage(self, event: dict) -> None:
        self.ledger.add(event)
        self._render_damage()
        self._save_totals(dict(event, type="damage"))

    def _render_damage(self):
        self.damage_readings = self.ledger.readings
        self.reading_sum = self.total_damage = self.ledger.total
        self.txt_damage.config(state="normal")
        self.txt_damage.delete("1.0", "end")
        for number, reading in enumerate(self.damage_readings, 1):
            value, label = reading["value"], reading["label"]
            amount = self.ledger.amount(reading)
            if amount is None:
                reason = "残缺" if reading.get("unresolved", 0) else "未知"
                line = f"{number:02d}  [{reason}] {reading.get('text', value)}\n     未计入\n"
                tag = "pending"
            elif label == "平均":
                line = f"{number:02d}  {value:,} × {self.ledger.enemies}\n     {amount:,}\n"
                tag = "confirmed"
            else:
                line = f"{number:02d}  合计  {amount:,}\n"
                tag = "confirmed"
            self.txt_damage.insert("end", line, tag)
        self.txt_damage.see("end")
        self.txt_damage.config(state="disabled")
        total_text = f"{self.total_damage:,}"
        font = getattr(self, "_total_font", None)
        if font is not None:
            available = self.lbl_damage_sum.winfo_width()
            if available <= 1:
                available = self.cfg.width - 48
            for size in range(18, 9, -1):
                font.configure(size=size)
                if font.measure(total_text) <= available:
                    break
        self.lbl_damage_sum.config(text=total_text)
        pending = self.ledger.pending
        self.lbl_total_damage.config(text=f"已确认 {len(self.damage_readings) - pending} 笔 · 待确认 {pending} 笔")
        self._render_resources()

    def _save_totals(self, event):
        monitor = getattr(self, "monitor", None)
        if monitor is None:
            return
        record = dict(event, enemy_count=self.ledger.enemies,
                      total=self.ledger.total, time=time.time(),
                      resources=asdict(resource_state(self.cfg.dp_max, self.cfg.hp_max,
                          self.ledger.total, self.ledger.last_damage)))
        try:
            path = Path(self.cfg.outdir) / f"session-{monitor.run_id}-totals.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._log(f"统计记录写入失败: {exc}", "err")

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
                elif item[0] == "stopped":
                    self._want_running = False
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
        self.lbl_target.config(text="HBR · 画面异常" if stats.get("broken") else "HBR · 识别中",
                               fg=WARN if stats.get("broken") else OK)
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
        if tag in ("err", "warn") or "[出错]" in line:
            self.lbl_warn.config(text=line[:140], fg=ERR if tag == "err" or "[出错]" in line else WARN)
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
        if self._want_running or (self.thread is not None and self.thread.is_alive()):
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        """开始。窗口还没开也没关系 —— 会一直重试到游戏出现。"""
        if self.thread is not None and self.thread.is_alive():
            return
        if self._want_running:
            return
        try:
            self.ledger.set_enemies(self._enemy_var.get())
        except ValueError as exc:
            self.lbl_enemy_hint.config(text=str(exc), fg=ERR)
            return
        self.damage_readings.clear()
        self.ledger = DamageLedger(enemies=self.ledger.enemies)
        self.reading_sum = self.total_damage = 0
        self.txt_damage.config(state="normal")
        self.txt_damage.delete("1.0", "end")
        self.txt_damage.config(state="disabled")
        self.lbl_damage_sum.config(text="0")
        self.lbl_total_damage.config(text="已确认 0 笔 · 待确认 0 笔")
        self._render_resources()
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
        self.lbl_target.config(text="HBR · 连接中", fg=FG)
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
            summary = self.monitor.run()
            if summary and summary.get("stopped_by_user"):
                self.queue.put(("stopped",))
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
        self.lbl_target.config(text="已停止 · 可自由拖动", fg=DIM)
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
        phys_h = self._desired_height()
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
        phys_w = compute_panel_width(game_rect, phys_w, screen_w)
        self.lbl_warn.config(wraplength=phys_w - 24)

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
                text="",
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
        if not self._want_running:
            return False
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
