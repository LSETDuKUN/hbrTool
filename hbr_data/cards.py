"""Rounded, scrollable record and skill cards; no JSON syntax in normal details."""
import tkinter as tk
from tkinter import ttk
from hbr_capture.widget_ui import RoundedCard

BG, CARD, FG, DIM, PINK = "#201d32", "#302b46", "#f7f0ff", "#bdb1d4", "#f5aacb"
WORDS = {"str":"力量","dex":"灵巧","wis":"智慧","spr":"精神","luk":"运气","con":"体力",
         "Attacker":"攻击手","Breaker":"破盾手","Blaster":"破坏手","Buffer":"增益手","Debuffer":"减益手","Healer":"治疗者","Defender":"守护者",
         "Slash":"斩","Stab":"突","Strike":"打","Fire":"火","Ice":"冰","Thunder":"雷","Light":"光","Dark":"暗",
         "AttackSkill":"攻击","DefenseDown":"防御降低","AttackUp":"攻击提升","Heal":"回复",
         "HealDp":"DP 回复","HealHp":"HP 回复","CriticalRateUp":"暴击率提升","CriticalDamageUp":"暴击伤害提升",
         "Single":"单体","All":"全体","Self":"自身","Main":"主伤害","Before":"命中前","After":"命中后"}


def number(value):
    if value is None:
        return "未提供"
    if isinstance(value,(int,float)):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return str(value)


def ratios(hits):
    return " + ".join(number(hit.ratio) for hit in hits)


def friendly(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return "；".join(f"{WORDS.get(k,k)}：{friendly(v)}" for k,v in value.items())
    if isinstance(value,(tuple,list)):
        return "、".join(friendly(v) for v in value) or "—"
    return WORDS.get(str(value),number(value))


class DetailCards(tk.Frame):
    def __init__(self,parent):
        super().__init__(parent,bg=BG)
        self.canvas = tk.Canvas(self,bg=BG,highlightthickness=0)
        scroll = ttk.Scrollbar(self,command=self.canvas.yview)
        scroll.pack(side="right",fill="y")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(fill="both",expand=True)
        self.body = tk.Frame(self.canvas,bg=BG)
        self.slot = self.canvas.create_window(0,0,window=self.body,anchor="nw")
        self.canvas.bind("<Configure>",lambda e:self.canvas.itemconfigure(self.slot,width=e.width))
        self.body.bind("<Configure>",lambda e:self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.photos = []
        self.card_count = 0

    def label(self,parent,text,size=10,color=FG,bold=False):
        label = tk.Label(parent,text=text,bg=parent.cget("bg"),fg=color,justify="left",anchor="w",
                         font=("Microsoft YaHei UI",size,"bold" if bold else "normal"))
        label.pack(fill="x",pady=3)
        label.bind("<Configure>",lambda e:label.configure(wraplength=max(100,e.width-8)) if str(label.cget("wraplength")) != str(max(100,e.width-8)) else None)
        return label

    def card(self,title):
        shell = RoundedCard(self.body,background=CARD)
        shell.pack(fill="x",padx=10,pady=7)
        self.label(shell.content,title,13,PINK,True)
        return shell.content

    def row(self,parent,title,value):
        line = tk.Frame(parent,bg=parent.cget("bg"))
        line.pack(fill="x",pady=2)
        tk.Label(line,text=title,bg=line.cget("bg"),fg=DIM,anchor="nw",width=12,
                 font=("Microsoft YaHei UI",9)).pack(side="left",anchor="n")
        self.label(line,value,10,FG,True)

    def fold(self,parent,title,fill):
        panel = tk.Frame(parent,bg=parent.cget("bg"))
        panel.pack(fill="x",pady=3)
        content = tk.Frame(panel,bg=parent.cget("bg"))
        opened = False
        built = False
        def toggle():
            nonlocal opened,built
            opened = not opened
            button.configure(text=("▾  " if opened else "▸  ")+title)
            if opened:
                if not built:
                    fill(content);built=True;self.bind_wheel(content)
                content.pack(fill="x",padx=7,pady=4)
            else:
                content.pack_forget()
        button = tk.Button(panel,text="▸  "+title,command=toggle,bg=parent.cget("bg"),fg=PINK,
                           relief="flat",anchor="w",font=("Microsoft YaHei UI",9,"bold"))
        button.pack(fill="x")

    def hit_rows(self,parent,hits,title="逐 Hit 倍率"):
        if not hits:
            self.row(parent,title,"暂缺 · 保留空白，后续可录入")
            return
        text = ratios(hits)
        def fill(frame):
            self.label(frame,text,10,"#9ee4ef",True)
            for hit in hits:
                self.row(frame,f"第 {hit.index} Hit",f"×{number(hit.ratio)} · {WORDS.get(hit.kind,hit.kind) or '未标类型'}")
        if len(hits)>6:
            self.fold(parent,f"{title} · {len(hits)} 段",fill)
        else:
            self.row(parent,title,text)
            if any(hit.kind != "Main" for hit in hits):
                self.fold(parent,"命中类型与顺序",fill)

    def render_skill(self,skill,index):
        self.card_count += 1
        frame = self.card(f"✦  {index}  {skill.name}")
        self.label(frame,skill.description,10)
        self.row(frame,"Hit 数",number(skill.hit_count) if skill.hit_count is not None and skill.hit_count>=0 else "不适用 / 来源未定义")
        self.row(frame,"SP 消耗",number(skill.sp_cost))
        self.hit_rows(frame,skill.hits)
        for effect in skill.effects:
            area = tk.Frame(frame,bg="#39324f",padx=10,pady=8)
            area.pack(fill="x",pady=7)
            self.label(area,f"效果 {effect.index+1} · {WORDS.get(effect.kind,effect.kind) or '未标类型'}",10,"#9ee4ef",True)
            self.row(area,"基础威力" if (effect.kind or "").startswith("Attack") else "效果数值",
                     " ～ ".join(number(v) for v in effect.power) or "未提供")
            self.row(area,"上限属性差",number(effect.stat_difference))
            weights = " + ".join(f"{WORDS.get(k,k)} × {number(v)}" for k,v in effect.weights if v)
            self.row(area,"属性权重",weights or "无属性权重")
            self.row(area,"攻击属性"," · ".join(WORDS.get(v,v) for v in (effect.physical_type,*effect.elements) if v) or "无")
            self.row(area,"DP 倍率","×"+number(effect.dp_multiplier))
            self.row(area,"HP 倍率","×"+number(effect.hp_multiplier))
            self.row(area,"破坏倍率","×"+number(effect.destruction_multiplier))
            if effect.hits:
                self.hit_rows(area,effect.hits,"该效果逐 Hit")
            if effect.condition:
                self.row(area,"生效条件",effect.condition)
            def extra(panel,effect=effect):
                self.row(panel,"等级成长",friendly(effect.growth))
                for key in ("target_type","value","hit_condition","target_condition","effect"):
                    if effect.raw.get(key):
                        self.row(panel,{"target_type":"作用对象","value":"附加数值","effect":"特殊效果","hit_condition":"命中条件","target_condition":"目标条件"}[key],friendly(effect.raw[key]))
            self.fold(area,"成长与其他效果",extra)
        def meta(panel):
            for key,label in (("id","技能 ID"),("label","来源标签"),("max_level","等级上限"),("use_count","使用次数"),("cond","技能条件"),("overwrite_cond","替换条件")):
                self.row(panel,label,friendly(skill.raw.get(key)))
        self.fold(frame,"技能补充信息",meta)

    def render(self,record):
        for child in self.body.winfo_children():
            child.destroy()
        self.photos.clear();self.card_count=0
        data = record["raw"]
        frame = self.card(record.get("name") or "资料")
        header = tk.Frame(frame,bg=CARD)
        header.pack(fill="x")
        portrait = record.get("portrait") or {}
        if portrait.get("local_path"):
            try:
                from PIL import Image,ImageTk
                with Image.open(portrait["local_path"]) as source:
                    picture = source.convert("RGBA");picture.thumbnail((100,100),Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(picture,master=self)
                self.photos.append(photo)
                tk.Label(header,image=photo,bg=CARD).pack(side="left",anchor="n",padx=(0,12),pady=5)
            except (OSError,ValueError):
                self.label(frame,"头像暂不可用",9,DIM)
        elif record["dataset"] in {"styles","characters","enemies"}:
            self.label(frame,"图标未公开或尚未缓存",9,DIM)
        if isinstance(data,dict):
            info = tk.Frame(header,bg=CARD)
            info.pack(side="left",fill="x",expand=True)
            def identity(panel):
                for key,label in (("chara","角色"),("tier","稀有度"),("role","定位"),("team","部队")):
                    if data.get(key):self.label(panel,f"{label}  {friendly(data[key])}",9,DIM)
            if record["dataset"] == "skills":
                self.fold(info,"所属角色与风格",identity)
            else:
                identity(info)
            if record["dataset"] == "enemies":
                self.row(frame,"敌人类型","Boss" if (data.get("flags") or {}).get("is_boss") else "普通敌人")
                for key,label in (("dp","初始 DP"),("hp","初始 HP")):
                    self.row(frame,label,number((data.get("base_param") or {}).get(key)))
            if not record["skills"] and data.get("desc"):
                self.label(frame,data["desc"])
        for index,skill in enumerate(record["skills"],1):
            self.render_skill(skill,index)
        extras = self.card("资料备注")
        self.label(extras,"威力范围尚未代入等级、增益和敌人参数；缺失的逐 Hit 倍率不做推算。",9,DIM)
        def more(panel):
            self.row(panel,"来源",record["source"])
            self.row(panel,"服务器",record["region"])
            if isinstance(data,dict):
                for key,value in data.items():
                    if key not in {"skills","parts","hits"}:
                        self.row(panel,key,friendly(value))
        self.fold(extras,"来源与其他资料",more)
        self.canvas.yview_moveto(0)
        self.bind_wheel(self.body)

    def bind_wheel(self,widget):
        widget.bind("<MouseWheel>",lambda event:self.canvas.yview_scroll(-int(event.delta/120),"units"))
        for child in widget.winfo_children():
            self.bind_wheel(child)
