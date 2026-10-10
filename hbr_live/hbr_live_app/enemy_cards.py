"""Separate compact gauges for every enemy, with stable session numbers."""
import tkinter as tk
from tkinter import font as tkfont
from .components import rounded


class EnemyCard(tk.Canvas):
    def __init__(self, parent, number):
        super().__init__(parent, height=104, bg='#201d32', highlightthickness=0)
        self.number = number
        self.state = None
        self.font = tkfont.Font(family='Microsoft YaHei UI', size=-12)
        self.bind('<Configure>', lambda _: self.draw())

    def update_state(self, state):
        self.state = dict(state)
        self.draw()

    def draw(self):
        self.delete('all')
        if not self.state:
            return
        s, w = self.state, max(100, self.winfo_width())
        rounded(self, 1, 1, w-1, 103, 16, fill='#302b46', outline='#50415f')
        departed = '（已离场）' if not s.get('active', True) else ''
        title = f'敌{self.number}{departed} · {s["name"]}'
        while self.font.measure(title) > w-24 and len(title) > 8:
            title = title[:-2] + '…'
        self.create_text(12, 16, text=title, anchor='w', fill='#f5aacb', font=self.font)
        for pool, y, color in [('dp', 40, '#83e5ed'), ('hp', 71, '#ffd398')]:
            change = s[pool+'_change']
            left = pool.upper() + (f'  {change:+,}' if change else '')
            self.create_text(12, y, text=left, anchor='w', fill=color, font=self.font)
            self.create_text(w-12, y, text=f'{s[pool]:,}', anchor='e', fill='#f7f0ff',
                             font=('Segoe UI', -14, 'bold italic'))
            rounded(self, 12, y+10, w-12, y+21, 5, fill='#201d32', outline='')
            fraction = min(1, s[pool]/max(1, s[pool+'_reference']))
            if fraction:
                rounded(self, 12, y+10, 12+(w-24)*fraction, y+21, 5, fill=color, outline='')
