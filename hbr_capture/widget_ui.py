"""Small canvas meters, drawn in physical pixels without image dependencies."""

import tkinter as tk
from tkinter import font as tkfont


def blend(first, second, ratio):
    a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * ratio):02x}" for x, y in zip(a, b))


class ResourceMeter(tk.Canvas):
    def __init__(self, master, name, subtitle, colors, **kwargs):
        super().__init__(master, height=106, bg="#1d2333", highlightthickness=0, **kwargs)
        self.name, self.subtitle, self.colors = name, subtitle, colors
        self.current = self.maximum = self.loss = 0
        self.number_font = tkfont.Font(family="Segoe UI", size=-18, weight="bold", slant="italic")
        self.loss_font = tkfont.Font(family="Segoe UI", size=-15, weight="bold", slant="italic")
        self.caption_font = tkfont.Font(family="Microsoft YaHei UI", size=-12)
        self.title_font = tkfont.Font(family="Segoe UI", size=-13, weight="bold")
        self.bind("<Configure>", lambda event: self.draw())

    def set_values(self, current, maximum, loss):
        self.current, self.maximum, self.loss = current, maximum, loss
        self.draw()

    def draw(self):
        self.delete("all")
        width = max(1, self.winfo_width())
        if width < 40:
            return
        pad, right = 10, width - 10
        fraction = self.current / self.maximum if self.maximum else 0
        fraction = max(0, min(1, fraction))
        self.create_rectangle(0, 0, 3, 106, fill=self.colors[0], outline="")
        self.create_text(pad, 15, text=f"{self.name}  {self.subtitle}", anchor="w",
                         fill=self.colors[1], font=self.title_font)
        self.create_text(right, 15, text=f"{fraction:.0%}", anchor="e",
                         fill="#94a5bf", font=self.caption_font)
        for font, text in ((self.loss_font, f"−{self.loss:,}"),
                           (self.number_font, f"{self.current:,}")):
            for size in range(18 if font is self.number_font else 15, 8, -1):
                font.configure(size=-size)
                if font.measure(text) <= (right - pad - 10) / 2:
                    break
        self.create_text(pad, 41, text=f"−{self.loss:,}", anchor="w",
                         fill="#f7bc88" if self.loss else "#8c9bb3", font=self.loss_font)
        self.create_text(right + 1, 42, text=f"{self.current:,}", anchor="e",
                         fill="#080c16", font=self.number_font)
        self.create_text(right, 41, text=f"{self.current:,}", anchor="e",
                         fill="#f0f6ff", font=self.number_font)
        self.create_rectangle(pad, 61, right, 77, fill="#101625", outline="#34415a")
        filled = int((right - pad - 2) * fraction)
        for x in range(filled):
            self.create_line(pad + 1 + x, 62, pad + 1 + x, 76,
                fill=blend(self.colors[0], self.colors[1], x / max(1, right - pad - 2)))
        if filled:
            self.create_line(pad + 1, 63, pad + filled, 63, fill=self.colors[1])
        for step in range(1, 5):
            x = pad + (right - pad) * step / 5
            self.create_line(x, 62, x, 76, fill="#1d2333")
        self.create_text(pad, 92, text="本次扣减", anchor="w", fill="#8495af", font=self.caption_font)
        self.create_text(right, 92, text=f"初始 {self.maximum:,}", anchor="e",
                         fill="#8495af", font=self.caption_font)
