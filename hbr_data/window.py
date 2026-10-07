"""Independent, non-blocking reference library for the capture widget."""
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk
from .store import Catalog
from .sync import sync

BG, CARD, FG, DIM, PINK = "#201d32", "#302b46", "#f7f0ff", "#bdb1d4", "#f5aacb"
LABELS = {"skills": "技能", "characters": "角色", "styles": "风格", "enemies": "敌人",
          "passives": "被动", "support_skills": "共鸣", "skill_types": "效果类型",
          "style_details": "风格详表", "enemy_details": "敌人详表", "master_skills": "大师技能",
          "rework_details": "调整历史", "localization": "本地化文本"}
EXAMPLE = """SELECT region, name, hit_count, part_index,
       power_min, power_max, diff_for_max,
       dp_multiplier, hp_multiplier, destruction_multiplier
FROM skill_damage
WHERE region = 'jp' AND power_max > 0
ORDER BY power_max DESC"""


class LibraryWindow:
    def __init__(self, master):
        self.catalog = Catalog()
        self.queue = queue.Queue()
        self.busy = False
        self.generation = 0
        self.selection_generation = 0
        self.offset = 0
        self.window = w = tk.Toplevel(master)
        w.title("星屑资料室 · HBR QUEST")
        w.configure(bg=BG)
        w.geometry(f"{min(1120,w.winfo_screenwidth()-100)}x{min(850,w.winfo_screenheight()-120)}")
        w.minsize(760, 560)
        w.protocol("WM_DELETE_WINDOW", w.withdraw)
        style = ttk.Style(w)
        style.configure("Quest.Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=30)
        style.configure("Quest.Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        tk.Label(w, text="✦  星屑资料室", font=("Microsoft YaHei UI", 20, "bold"), bg=BG, fg=PINK).pack(anchor="w", padx=22, pady=(18, 2))
        self.status = tk.StringVar(value="本地资料 · 查询无需联网")
        top = tk.Frame(w, bg=BG)
        top.pack(fill="x", padx=22, pady=(0,12))
        tk.Label(top, textvariable=self.status, bg=BG, fg=DIM, anchor="w").pack(side="left", fill="x", expand=True)
        self.update_button = tk.Button(top, text="同步网站资料", command=self.update, bg=PINK, relief="flat", padx=12)
        self.update_button.pack(side="right")
        self.tabs = ttk.Notebook(w)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0,18))
        browse = tk.Frame(self.tabs, bg=BG)
        advanced = tk.Frame(self.tabs, bg=BG)
        coverage = tk.Frame(self.tabs, bg=BG)
        self.tabs.add(browse, text="  查资料  ")
        self.tabs.add(advanced, text="  SQL 查询  ")
        self.tabs.add(coverage, text="  来源与覆盖  ")
        bar = tk.Frame(browse, bg=BG)
        bar.pack(fill="x", pady=12)
        self.region = tk.StringVar(value="jp · 日服")
        ttk.Combobox(bar, textvariable=self.region, values=["jp · 日服", "cn · 国服", "en · 国际服"], state="readonly", width=13).pack(side="left", padx=5)
        self.dataset = tk.StringVar(value="skills · 技能")
        self.dataset_box = ttk.Combobox(bar, textvariable=self.dataset, state="readonly", width=24)
        self.dataset_box.pack(side="left", padx=5)
        self.keyword = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self.keyword, bg=CARD, fg=FG, insertbackground=FG, relief="flat")
        entry.pack(side="left", fill="x", expand=True, ipady=7, padx=5)
        entry.bind("<Return>", lambda e: self.search())
        tk.Button(bar, text="搜索", command=self.search, bg=PINK, relief="flat").pack(side="left", padx=5)
        tk.Label(browse, text="按名称、ID、角色名或原文内容搜索；不同服务器独立保存。双击/选择记录查看所有字段。", bg=BG, fg=DIM, anchor="w").pack(fill="x", padx=6)
        panes = tk.PanedWindow(browse, orient="horizontal", bg=BG, sashwidth=8)
        panes.pack(fill="both", expand=True, pady=8)
        left = tk.Frame(panes, bg=BG)
        right = ttk.Notebook(panes)
        panes.add(left, minsize=230, width=340)
        panes.add(right, minsize=340)
        self.tree = self.make_tree(left, ("id", "name"))
        self.tree.bind("<<TreeviewSelect>>", self.select)
        paging = tk.Frame(left, bg=BG)
        paging.pack(fill="x")
        tk.Button(paging, text="上一页", command=lambda: self.page(-1), relief="flat").pack(side="left")
        tk.Button(paging, text="下一页", command=lambda: self.page(1), relief="flat").pack(side="right")
        self.page_label = tk.Label(paging, bg=BG, fg=DIM)
        self.page_label.pack()
        self.summary = self.make_text(right)
        self.raw = self.make_text(right)
        right.add(self.summary.master, text="  数值与说明  ")
        right.add(self.raw.master, text="  完整 JSON  ")
        tk.Label(advanced, text="只读 SQLite · 单次最多 500 行 / 3 秒；支持 JOIN、聚合、JSON 查询。所有原始字段位于 raw_json。", bg=BG, fg=DIM).pack(anchor="w", padx=8, pady=8)
        self.sql = tk.Text(advanced, height=8, bg=CARD, fg=FG, insertbackground=FG, font=("Consolas", 11), relief="flat")
        self.sql.pack(fill="x", padx=8)
        self.sql.insert("1.0", EXAMPLE)
        tk.Button(advanced, text="运行查询", command=self.run_sql, bg=PINK, relief="flat").pack(anchor="e", padx=8, pady=8)
        self.sql_tree = self.make_tree(advanced, ("结果",))
        self.coverage = self.make_text(coverage)
        self.coverage.master.pack(fill="both", expand=True)
        self.refresh()
        w.after(100, self.pump)

    def make_text(self, parent):
        frame = tk.Frame(parent, bg=BG)
        text = tk.Text(frame, wrap="word", bg=CARD, fg=FG, insertbackground=FG, relief="flat", padx=16, pady=14, font=("Microsoft YaHei UI", 10))
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.tag_configure("title", foreground=PINK, font=("Microsoft YaHei UI", 15, "bold"))
        return text

    def make_tree(self, parent, columns):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Quest.Treeview")
        vertical = ttk.Scrollbar(frame, command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=180, minwidth=80)
        return tree

    def set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.tag_add("title", "1.0", "1.end")
        widget.configure(state="disabled")

    def task(self, fn, done):
        def work():
            try:
                self.queue.put((done, fn(), None))
            except Exception as exc:
                self.queue.put((done, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        try:
            while True:
                callback, value, error = self.queue.get_nowait()
                if error:
                    self.status.set("未完成：" + error)
                callback(value, error)
        except queue.Empty:
            pass
        if self.window.winfo_exists():
            self.window.after(100, self.pump)

    def refresh(self):
        try:
            manifest = self.catalog.manifest()
        except FileNotFoundError:
            self.status.set("尚无本地资料，请点击「同步网站资料」")
            return
        datasets = sorted({s["table"] for s in manifest["sources"].values()})
        self.dataset_box.configure(values=[f"{name} · {LABELS.get(name,name)}" for name in datasets])
        self.status.set(f"快照 {manifest['created'][:16]} UTC · {len(manifest['sources'])} 来源 · {len(manifest['errors'])} 不可用")
        counts = {}
        for source in manifest["sources"].values():
            key = source["region"] + "/" + source["table"]
            counts[key] = counts.get(key, 0) + 1
        report = "来源与覆盖报告\n\nhttps://hbr.quest/\nhttps://master.hbr.quest/v1/\n\n" + self.status.get()
        report += "\n\n范围：站点公开模块引用的 JSON 表及表中列出的详细资料。图片/音视频不下载。无法发现的服务器文件不声称已覆盖。\n原始 JSON、页面脚本、SHA256、下载时间与历史版本均保存在 data/quest。\n\n"
        report += "\n".join(f"{key}: {count} 文件" for key,count in sorted(counts.items()))
        report += "\n\n不可用来源（不隐瞒缺项）：\n" + "\n".join(f"{url}\n  {error}" for url,error in manifest["errors"].items())
        report += "\n\nSQL 表：sources, records, translations, skills, skill_parts, skill_hits, skill_elements\n视图：characters, styles, enemies, style_skills, skill_catalog, skill_damage\n关联：skills.skill_key = skill_parts.skill_key；source_url → sources.url。\n同一技能可能出现在多个来源；skill_catalog / skill_damage 限定技能主表，仍保留表内条件变体；全部来源用 skills。"
        self.set_text(self.coverage, report)
        self.search()

    def update(self):
        if self.busy:
            return
        self.busy = True
        self.update_button.configure(state="disabled")
        def progress(text):
            self.queue.put((lambda value, error: self.status.set(value), text, None))
        def done(value, error):
            self.busy = False
            self.update_button.configure(state="normal")
            if not error:
                self.refresh()
        def download():
            stage = self.catalog.root / "staging.json"
            resume = False
            if stage.exists():
                pending = json.loads(stage.read_bytes())
                try:
                    current = self.catalog.manifest()
                except FileNotFoundError:
                    current = {}
                resume = (pending.get("created") != current.get("created") or
                          pending.get("database") != current.get("database") or
                          any("HTTP Error 404:" not in e for e in pending.get("errors", {}).values()))
            return sync(self.catalog.root, progress=progress, resume=resume)
        self.task(download, done)

    def page(self, direction):
        self.offset = max(0, self.offset + direction * 250)
        self.search(reset=False)

    def search(self, reset=True):
        if reset:
            self.offset = 0
        self.generation += 1
        self.selection_generation += 1
        generation = self.generation
        region, dataset = self.region.get().split(" · ")[0], self.dataset.get().split(" · ")[0]
        keyword = self.keyword.get()
        pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        offset = self.offset
        def done(result, error):
            if error or generation != self.generation:
                return
            self.tree.delete(*self.tree.get_children())
            self.keys = {}
            for index, row in enumerate(result[1]):
                self.keys[str(index)] = row[0]
                self.tree.insert("", "end", iid=str(index), values=(row[1], row[2] or row[3]))
            self.page_label.configure(text=f"第 {offset//250+1} 页 · {len(result[1])} 条")
        self.task(lambda: self.catalog.query("SELECT record_key,id,COALESCE(NULLIF(name_zh,''),name),label FROM records WHERE region=? AND dataset=? AND (raw_json LIKE ? ESCAPE '\\' OR name_zh LIKE ? ESCAPE '\\') ORDER BY ordinal LIMIT 250 OFFSET ?", (region,dataset,pattern,pattern,offset)), done)

    def select(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return
        key = self.keys[selected[0]]
        self.selection_generation += 1
        generation = self.selection_generation
        def done(result, error):
            if error or generation != self.selection_generation or not result[1]:
                return
            raw, source, translated = result[1][0]
            data = json.loads(raw)
            self.set_text(self.raw, json.dumps(data, ensure_ascii=False, indent=2))
            lines = [str(data.get("name", data.get("label", "资料"))) if isinstance(data,dict) else "资料", "", "来源：" + source]
            if translated:
                lines.insert(1, "中文参考名：" + translated)
            def skill_info(skill):
                lines.extend(["", str(skill.get("name", "技能")), str(skill.get("desc", "")),
                    f"Hit 数：{skill.get('hit_count','未披露')}    SP：{skill.get('sp_cost','未披露')}    等级上限：{skill.get('max_level','未披露')}",
                    f"技能逐 Hit 分配：{skill.get('hits') or '来源未提供'}"])
                for index, part in enumerate(skill.get("parts", []), 1):
                    mult = part.get("multipliers") or {}
                    lines.extend([f"\n效果段 {index} · {part.get('skill_type')} · {part.get('type')} / {part.get('elements')}",
                        f"基础威力范围：{part.get('power','未披露')}",
                        f"达到上限的属性差：{part.get('diff_for_max','未披露')}",
                        f"DP 倍率 ×{mult.get('dp','?')}    HP 倍率 ×{mult.get('hp','?')}    破坏倍率 ×{mult.get('dr','?')}",
                        f"属性权重：{part.get('parameters')}", f"等级成长：{part.get('growth')}",
                        f"逐 Hit 数据：{part.get('hits')}", f"条件：{part.get('cond') or '无'}",
                        f"效果值：{part.get('value')}    特殊效果：{part.get('effect')}"])
            if isinstance(data, dict):
                if "hit_count" in data:
                    skill_info(data)
                else:
                    lines.extend(["", str(data.get("desc", "")), "角色：" + str(data.get("chara", ""))])
                    for skill in data.get("skills", []):
                        skill_info(skill)
                    if not data.get("skills"):
                        lines.append(json.dumps(data, ensure_ascii=False, indent=2))
            lines.extend(["", "基础威力不是实战伤害；buff、敌人防御、技能等级与条件等尚未代入。", "非攻击效果的 power 不表示伤害。空 hits 表示来源未给逐 Hit 分配，不擅自平均。", "所有未知字段和嵌套变体保留在「完整 JSON」。"])
            self.set_text(self.summary, "\n".join(lines))
        self.task(lambda: self.catalog.query("SELECT raw_json,source_url,name_zh FROM records WHERE record_key=?", (key,)), done)

    def run_sql(self):
        sql = self.sql.get("1.0", "end").strip()
        def done(result, error):
            if error:
                return
            columns, rows, truncated = result
            tree = self.sql_tree
            tree.delete(*tree.get_children())
            keys = [str(i) for i in range(len(columns))]
            tree.configure(columns=keys)
            for key, title in zip(keys, columns):
                tree.heading(key, text=title)
                tree.column(key, width=160, minwidth=70)
            for row in rows:
                tree.insert("", "end", values=["NULL" if v is None else v for v in row])
            self.status.set(f"查询成功 · {len(rows)} 行" + ("（仅显示前 500 行，请缩小条件）" if truncated else ""))
        self.task(lambda: self.catalog.query(sql), done)


if __name__ == "__main__":
    from hbr_capture.win32 import set_dpi_aware
    set_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    window = LibraryWindow(root)
    window.window.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
