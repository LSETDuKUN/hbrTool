"""Standalone battle dashboard. All Tk access stays on the main thread."""
import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from .components import RoundedCard, RoundedButton, ResourceMeter
from .model import BattleModel

ROOT = Path(__file__).resolve().parent.parent
BG, CARD, FG, DIM = '#201d32', '#302b46', '#f7f0ff', '#bdb1d4'
PINK, CYAN, GOLD = '#f5aacb', '#83e5ed', '#ffd398'
FONT = ('Microsoft YaHei UI', -14)


class App:
    def __init__(self, root=None):
        self.root = root or tk.Tk()
        self.model = BattleModel()
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.thread = None
        self.connected = False
        self.closing = False
        self.library = None
        self.enemy_keys = []
        self.log_lines = []
        self.last_packet = None
        self.last_sample = None
        self.session_path = None
        self.settings = self._load_settings()
        self.compact = tk.BooleanVar(value=bool(self.settings.get('compact', False)))
        self.topmost = tk.BooleanVar(value=bool(self.settings.get('topmost', True)))
        self.opacity = tk.DoubleVar(value=max(.7, min(1., float(self.settings.get('opacity', 1)))))
        self.root.title('HBR Live · 星屑战斗手帐')
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.attributes('-topmost', self.topmost.get())
        self.root.attributes('-alpha', self.opacity.get())
        self.style()
        self.build()
        self.layout()
        self._restore_geometry()
        self.root.after(60, self.pump)

    def _load_settings(self):
        try:
            data = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _restore_geometry(self):
        default = (420, 740) if self.compact.get() else (900, 790)
        values = self.settings.get('bounds', [*default, 80, 60])
        try:
            w, h, x, y = map(int, values)
        except (TypeError, ValueError):
            w, h, x, y = *default, 80, 60
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(max(390, w), sw-30), min(max(650, h), sh-70)
        x, y = max(0, min(x, sw-w-16)), max(0, min(y, sh-h-60))
        self.root.geometry(f'{w}x{h}+{x}+{y}')

    def style(self):
        style = ttk.Style(self.root)
        if style.theme_use() not in ('clam', 'quest'):
            style.theme_use('clam')
        style.configure('Live.Treeview', background=CARD, fieldbackground=CARD,
                        foreground=FG, rowheight=42, borderwidth=0, font=FONT)
        style.configure('Live.Treeview.Heading', background='#493752', foreground=PINK,
                        font=(*FONT, 'bold'), relief='flat')
        style.map('Live.Treeview', background=[('selected', '#605077')], foreground=[('selected', '#ffffff')])
        style.configure('Live.TCombobox', fieldbackground=CARD, foreground=FG, background=CARD)
        style.map('Live.TCombobox', fieldbackground=[('readonly', CARD)], foreground=[('readonly', FG)])

    def label(self, parent, text='', color=FG, size=10, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color,
                        font=('Microsoft YaHei UI', -round(size * 1.35)), **kwargs)

    def build(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill='x', padx=22, pady=(18, 8))
        self.label(head, '✦  HBR LIVE', PINK, 19).pack(side='left')
        self.subtitle = self.label(head, '星屑战斗手帐', DIM, 10)
        self.subtitle.pack(side='left', padx=12)
        menu_button = tk.Menubutton(head, text='设置 ⋯', bg=CARD, fg=FG, relief='flat', padx=12, font=FONT)
        menu_button.pack(side='right')
        menu = tk.Menu(menu_button, tearoff=False, bg=CARD, fg=FG)
        menu_button.configure(menu=menu)
        menu.add_checkbutton(label='紧凑模式', variable=self.compact, command=self.layout)
        menu.add_checkbutton(label='保持置顶', variable=self.topmost,
                             command=lambda: self.root.attributes('-topmost', self.topmost.get()))
        for value, title in ((1., '不透明'), (.9, '透明度 90%'), (.8, '透明度 80%')):
            menu.add_radiobutton(label=title, variable=self.opacity, value=value,
                                command=lambda: self.root.attributes('-alpha', self.opacity.get()))
        menu.add_separator()
        menu.add_command(label='导出本场摘要…', command=self.export)
        menu.add_command(label='打开记录文件夹', command=self.open_sessions)
        menu.add_command(label='诊断日志', command=self.show_logs)
        self.hero = RoundedCard(self.root)
        self.hero.pack(fill='x', padx=18, pady=6)
        hero = self.hero.content
        self.label(hero, '本场显示伤害', DIM, 10).pack(anchor='w')
        self.total_label = tk.Label(hero, text='0', bg=CARD, fg=PINK,
                                   font=('Segoe UI', -46, 'bold italic'), anchor='w')
        self.total_label.pack(fill='x')
        self.summary = self.label(hero, '尚未连接  ·  等待第一笔行动', DIM)
        self.summary.pack(anchor='w')
        controls = tk.Frame(self.root, bg=BG)
        controls.pack(fill='x', padx=18, pady=(8, 4))
        for col in range(3):
            controls.columnconfigure(col, weight=1, uniform='controls')
        self.start_button = RoundedButton(controls, '连接战斗', self.start_or_stop, PINK, BG, (*FONT, 'bold'))
        self.start_button.grid(row=0, column=0, sticky='ew', padx=(0, 5))
        self.pause_button = RoundedButton(controls, '暂停', self.toggle_pause, '#4b4163', FG, FONT)
        self.pause_button.grid(row=0, column=1, sticky='ew', padx=5)
        RoundedButton(controls, '星屑资料室', self.open_library, '#4b4163', FG, FONT).grid(row=0, column=2, sticky='ew', padx=(5, 0))
        self.status = self.label(self.root, '先进入战斗指令选择画面，再连接。窗口可随时拖动。', DIM,
                                 anchor='w', justify='left', wraplength=800)
        self.status.pack(fill='x', padx=24, pady=(3, 8))
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill='both', expand=True, padx=18)
        self.resources = tk.Frame(self.body, bg=BG)
        self.label(self.resources, '敌方状态', CYAN, 13).pack(anchor='w', padx=8, pady=(2, 10))
        self.enemy_var = tk.StringVar()
        self.enemy_box = ttk.Combobox(self.resources, textvariable=self.enemy_var,
                                    state='readonly', style='Live.TCombobox', font=FONT)
        self.enemy_box.pack(fill='x', padx=6, pady=(0, 8))
        self.enemy_box.bind('<<ComboboxSelected>>', lambda _: self.render_enemy())
        self.dp = ResourceMeter(self.resources, 'DP', '防护', ('#6178ef', CYAN))
        self.dp.pack(fill='x', pady=4)
        self.hp = ResourceMeter(self.resources, 'HP', '生命', ('#f190ae', GOLD))
        self.hp.pack(fill='x', pady=4)
        self.resource_note = self.label(self.resources, '等待敌方状态', DIM, 9, anchor='w', justify='left', wraplength=280)
        self.resource_note.pack(fill='x', padx=8, pady=8)
        self.label(self.resources, '数值直接读取游戏\n显示伤害包含溢出，不用于推算扣血。', DIM, 9, wraplength=270,
                   anchor='w', justify='left').pack(fill='x', padx=8, pady=8)
        self.history = tk.Frame(self.body, bg=BG)
        self.label(self.history, '行动记录', PINK, 13).pack(anchor='w', pady=(2, 10))
        tree_wrap = tk.Frame(self.history, bg=CARD)
        tree_wrap.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=('actor', 'skill', 'damage'), show='headings',
                                 selectmode='browse', style='Live.Treeview', height=4)
        for key, title, width in [('actor', '行动者', 100), ('skill', '技能', 155), ('damage', '显示伤害', 125)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=65, anchor='e' if key == 'damage' else 'w')
        scrollbar = ttk.Scrollbar(tree_wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.render_details())
        self.tree.bind('<Double-1>', lambda _: self.show_details())
        self.detail_caption = self.label(self.history, '选择一笔行动查看命中明细；双击可单独展开。', DIM, 9, anchor='w', wraplength=420, justify='left')
        self.detail_caption.pack(fill='x', pady=8)
        self.detail_wrap = tk.Frame(self.history, bg=CARD)
        self.detail = tk.Text(self.detail_wrap, height=6, bg=CARD, fg=FG, relief='flat', font=FONT,
                              wrap='word', padx=12, pady=10, state='disabled')
        detail_scroll = ttk.Scrollbar(self.detail_wrap, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side='right', fill='y')
        self.detail.pack(fill='both', expand=True)
        self.detail_wrap.pack(fill='x')
        self.history.bind('<Configure>', lambda e: self.detail_caption.configure(wraplength=max(200,e.width-10)))
        self.footer = self.label(self.root, '本地记录  ·  只读内存  ·  无需截图', DIM, 9, anchor='w')
        self.footer.pack(side='bottom', fill='x', padx=24, pady=(10, 14), before=self.body)
        self.root.bind('<Configure>', self.on_resize, add='+')

    def on_resize(self, event):
        if event.widget is self.root:
            self.status.configure(wraplength=max(300, event.width-50))
            self.total_label.configure(font=('Segoe UI', -36 if event.width < 500 else -46, 'bold italic'))

    def layout(self):
        self.resources.grid_forget()
        self.history.grid_forget()
        if self.compact.get():
            for key, width in (('actor', 75), ('skill', 120), ('damage', 135)):
                self.tree.column(key, width=width, stretch=key != 'damage')
            self.root.minsize(390, 820)
            self.body.columnconfigure(0, weight=1)
            self.body.columnconfigure(1, weight=0)
            self.body.rowconfigure(0, weight=0)
            self.body.rowconfigure(1, weight=1)
            self.resources.pack_propagate(True)
            self.subtitle.pack_forget()
            self.resources.grid(row=0, column=0, sticky='ew')
            self.history.grid(row=1, column=0, sticky='nsew', pady=(8, 0))
            self.detail_wrap.pack_forget()
            self.detail_caption.pack_forget()
            self.resource_note.pack_forget()
            self.dp.configure(height=106)
            self.root.geometry(f'420x{min(850, self.root.winfo_screenheight()-90)}')
        else:
            for key, width in (('actor', 100), ('skill', 155), ('damage', 145)):
                self.tree.column(key, width=width, stretch=key != 'damage')
            self.root.minsize(780, 730)
            self.body.columnconfigure(0, weight=0, minsize=295)
            self.body.columnconfigure(1, weight=1)
            self.body.rowconfigure(0, weight=1)
            self.body.rowconfigure(1, weight=0)
            self.resources.configure(width=300)
            self.resources.pack_propagate(False)
            self.subtitle.pack(side='left', padx=12)
            self.resources.grid(row=0, column=0, sticky='nsew', padx=(0, 16))
            self.history.grid(row=0, column=1, sticky='nsew')
            self.detail_caption.pack(fill='x', pady=8)
            self.detail_wrap.pack(fill='x')
            self.resource_note.pack(fill='x', padx=8, pady=8)
            self.root.geometry('900x790')

    def start_or_stop(self):
        if self.thread and self.thread.is_alive():
            self.stop_event.set()
            self.set_status('正在停止并保存记录…')
            return
        self.stop_event.clear()
        self.paused.clear()
        self.connected = False
        self.pause_button.configure(text='暂停')
        self.start_button.configure(text='取消连接')
        self.set_status('正在连接，请暂不行动…')
        from .source import run
        self.thread = threading.Thread(target=run,
            args=(self.stop_event, self.paused, self.queue.put, ROOT / 'sessions'), daemon=True)
        self.thread.start()

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.start_or_stop()

    def toggle_pause(self):
        if not self.connected:
            self.set_status('连接成功后可暂停。', DIM)
            return
        if self.paused.is_set():
            self.paused.clear()
            self.pause_button.configure(text='暂停')
            self.set_status('正在读取 · 可以行动', CYAN)
        else:
            self.paused.set()
            self.pause_button.configure(text='继续')
            self.set_status('已暂停 · 思考期间请勿行动；继续后沿用本场记录。', GOLD)

    def pump(self):
        try:
            # Bound each pass so long bursts cannot starve Tk input.
            for _ in range(200):
                kind, data = self.queue.get_nowait()
                if kind == 'connected':
                    self.model = BattleModel()
                    self.model.run_id = data['run_id']
                    self.session_path = data['path']
                    self.tree.delete(*self.tree.get_children())
                    self.enemy_keys = []
                    self.last_sample = None
                    self.connected = True
                    self.start_button.configure(text='停止')
                    self.set_status('内存已连接 · 可以行动', CYAN)
                elif kind == 'snapshot':
                    self.model.update(data)
                    self.last_packet = data
                    self.render(data.get('events', []))
                elif kind == 'heartbeat':
                    self.last_sample = data
                    self.footer.configure(text=f'最近采样 {time.strftime("%H:%M:%S", time.localtime(data))}  ·  本场记录自动保存')
                elif kind == 'stopped':
                    self.connected = False
                    self.start_button.configure(text='连接新战斗')
                    self.pause_button.configure(text='暂停')
                elif kind == 'error':
                    self.set_status(data, '#ffadb7')
                elif kind == 'status':
                    self.set_status(data)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(60, self.pump)

    def render(self, events=()):
        self.total_label.configure(text=f'{self.model.total:,}')
        biggest = max((e['value'] for e in self.model.actions.values()), default=0)
        self.summary.configure(text=f'{len(self.model.actions)} 次行动  ·  单次最高 {biggest:,}')
        keys = list(self.model.enemies)
        if keys != self.enemy_keys:
            self.enemy_keys = keys
            self.enemy_box.configure(values=[f'{n+1} · {self.model.enemies[key]["name"]}' for n, key in enumerate(keys)])
            if keys:
                self.enemy_box.current(0)
        self.render_enemy()
        for event in events:
            iid = str(event['event_id'])
            values = (event['actor'], event['skill'], f'{event["value"]:,}' + (' …' if not event['settled'] else ''))
            if self.tree.exists(iid):
                self.tree.item(iid, values=values)
            else:
                self.tree.insert('', 'end', iid=iid, values=values)
                self.tree.see(iid)
            if not self.tree.selection():
                self.tree.selection_set(iid)
        self.render_details()

    def render_enemy(self):
        index = self.enemy_box.current()
        if not 0 <= index < len(self.enemy_keys):
            return
        state = self.model.enemies[self.enemy_keys[index]]
        self.dp.set_values(state['dp'], state['dp_reference'], -state['dp_change'])
        self.hp.set_values(state['hp'], state['hp_reference'], -state['hp_change'])
        self.resource_note.configure(text=f'DP 回升 {state["dp_rises"]} 次 · HP 回升 {state["hp_rises"]} 次\n量表参考值：本次连接以来观测到的最大值。')

    def detail_text(self):
        selected = self.tree.selection()
        if not selected:
            return '行动后可在这里查看本体、连击珠、暴击与目标。'
        event = self.model.actions.get(int(selected[0]))
        if event is None:
            return ''
        text = f'{event["actor"]} · {event["skill"]}\n显示伤害 {event["value"]:,}  ·  {"已结算" if event["settled"] else "命中进行中"}\n\n'
        for n, hit in enumerate(event['hits'], 1):
            enemy = self.model.enemies.get(hit['target'], {})
            target_index = self.enemy_keys.index(hit['target'])+1 if hit['target'] in self.enemy_keys else '?'
            kind = '连击珠' if hit['funnel'] else '本体'
            crit = ' · 暴击' if hit['critical'] else ''
            text += f'{n:02d}  {kind}{crit}    {hit["damage"]:,}\n      → 敌人 {target_index} · {enemy.get("name", "未知目标")}\n'
        return text

    def render_details(self):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('end', self.detail_text())
        self.detail.configure(state='disabled')

    def show_details(self):
        self.text_window('命中明细', self.detail_text())

    def text_window(self, title, text):
        window = tk.Toplevel(self.root)
        window.title('HBR Live · ' + title)
        window.geometry('580x500')
        box = tk.Text(window, bg=BG, fg=FG, font=FONT, wrap='word', padx=20, pady=20)
        scroll = ttk.Scrollbar(window, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        box.pack(fill='both', expand=True)
        box.insert('end', text)
        box.configure(state='disabled')

    def open_library(self):
        from hbr_data.window import LibraryWindow
        if self.library is None or not self.library.window.winfo_exists():
            self.library = LibraryWindow(self.root)
            self.style()
        self.library.window.deiconify()
        self.library.window.lift()

    def set_status(self, text, color=DIM):
        self.status.configure(text=text, fg=color)
        self.log_lines.append(time.strftime('%H:%M:%S') + '  ' + text)
        self.log_lines = self.log_lines[-300:]

    def show_logs(self):
        self.text_window('诊断日志', '\n'.join(self.log_lines) or '暂无日志')

    def open_sessions(self):
        path = ROOT / 'sessions'
        path.mkdir(exist_ok=True)
        os.startfile(str(path))

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='导出本场摘要',
            initialfile=(self.model.run_id or 'battle') + '.json', defaultextension='.json',
            filetypes=[('战斗摘要', '*.json')])
        if path:
            try:
                Path(path).write_text(json.dumps(self.model.export(), ensure_ascii=False, indent=2), encoding='utf-8')
                self.set_status('摘要已导出')
            except OSError as exc:
                messagebox.showerror('导出失败', str(exc), parent=self.root)

    def close(self):
        if not self.closing:
            self.closing = True
            self.stop_event.set()
            settings = dict(compact=self.compact.get(), topmost=self.topmost.get(), opacity=self.opacity.get(),
                bounds=[self.root.winfo_width(), self.root.winfo_height(), self.root.winfo_x(), self.root.winfo_y()])
            try:
                temp = ROOT / 'settings.json.tmp'
                temp.write_text(json.dumps(settings), encoding='utf-8')
                temp.replace(ROOT / 'settings.json')
            except OSError:
                pass
        if self.thread and self.thread.is_alive():
            self.status.configure(text='正在保存并关闭…')
            self.root.after(100, self.close)
        else:
            self.root.destroy()
