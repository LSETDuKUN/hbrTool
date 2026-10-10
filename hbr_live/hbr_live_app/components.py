"""Small canvas meters, drawn in physical pixels without image dependencies."""

import tkinter as tk
from tkinter import font as tkfont


def rounded(canvas, x1, y1, x2, y2, radius=16, **kwargs):
    r = min(radius, (x2-x1)/2, (y2-y1)/2)
    return canvas.create_polygon(x1+r,y1,x2-r,y1,x2,y1,x2,y1+r,
        x2,y2-r,x2,y2,x2-r,y2,x1+r,y2,x1,y2,x1,y2-r,
        x1,y1+r,x1,y1, smooth=True, splinesteps=24, **kwargs)


class RoundedCard(tk.Canvas):
    """Rounded shell whose inner frame keeps normal Tk geometry management."""
    def __init__(self, master, background="#302b46", **kwargs):
        super().__init__(master, bg=master.cget("bg"), highlightthickness=0, **kwargs)
        self.background = background
        self.content = tk.Frame(self, bg=background, padx=12, pady=9)
        self.slot = self.create_window(8, 7, anchor="nw", window=self.content)
        self.content.bind("<Configure>", self._layout)
        self.bind("<Configure>", self._layout)

    def _layout(self, event=None):
        width = max(40, self.winfo_width())
        self.itemconfigure(self.slot, width=width-16)
        height = self.content.winfo_reqheight()+14
        self.configure(height=height)
        self.delete("shell")
        rounded(self, 1, 1, width-1, height-1, 20, fill=self.background, outline="#50415f", tags="shell")
        self.tag_lower("shell")


class RoundedButton(tk.Canvas):
    def __init__(self, master, text, command, bg, fg, font, **kwargs):
        self._text, self._color, self._fg = text, bg, fg
        self._font, self._command = font, command
        super().__init__(master, height=44, bg=master.cget("bg"), highlightthickness=0,
                         takefocus=True, cursor="hand2")
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", lambda e: self._command())
        self.bind("<Return>", lambda e: self._command())
        self.bind("<space>", lambda e: self._command())
        self.bind("<FocusIn>", lambda e: self.draw())
        self.bind("<FocusOut>", lambda e: self.draw())

    def configure(self, cnf=None, **kwargs):
        for option, attr in (("text","_text"),("bg","_color"),("fg","_fg")):
            if option in kwargs:
                setattr(self,attr,kwargs.pop(option))
        result = super().configure(cnf, **kwargs)
        self.draw()
        return result

    config = configure

    def draw(self):
        self.delete("all")
        width = self.winfo_width()
        rounded(self, 1, 1, width-1, 43, 22, fill=self._color,
                outline="#fff0fa" if self.focus_get() is self else "")
        self.create_text(width/2,22,text=self._text,font=self._font,fill=self._fg)


def blend(first, second, ratio):
    a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * ratio):02x}" for x, y in zip(a, b))


class ResourceMeter(tk.Canvas):
    def __init__(self, master, name, subtitle, colors, **kwargs):
        super().__init__(master, height=106, bg=master.cget("bg"), highlightthickness=0, **kwargs)
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
        rounded(self, 1, 1, width-1, 105, 20, fill="#302b46", outline="#50415f")
        self.create_oval(width-60, 3, width-18, 25, fill="#3c344f", outline="")
        self.create_text(pad, 15, text=f"{self.name}  {self.subtitle}", anchor="w",
                         fill=self.colors[1], font=self.title_font)
        self.create_text(right, 15, text=f"{fraction:.0%}", anchor="e",
                         fill="#94a5bf", font=self.caption_font)
        for font, text in ((self.loss_font, f"{'−' if self.loss >= 0 else '+'}{abs(self.loss):,}"),
                           (self.number_font, f"{self.current:,}")):
            for size in range(18 if font is self.number_font else 15, 8, -1):
                font.configure(size=-size)
                if font.measure(text) <= (right - pad - 10) / 2:
                    break
        self.create_text(pad, 41, text=f"{'−' if self.loss >= 0 else '+'}{abs(self.loss):,}", anchor="w",
                         fill="#f7bc88" if self.loss else "#8c9bb3", font=self.loss_font)
        self.create_text(right + 1, 42, text=f"{self.current:,}", anchor="e",
                         fill="#080c16", font=self.number_font)
        self.create_text(right, 41, text=f"{self.current:,}", anchor="e",
                         fill="#f0f6ff", font=self.number_font)
        rounded(self, pad, 59, right, 79, 10, fill="#201d32", outline="#55466d")
        filled = int((right - pad - 2) * fraction)
        for x in range(filled):
            # Clip the gradient to the capsule ends, including nearly empty bars.
            radius = min(8, filled / 2)
            edge = min(x, filled-1-x)
            inset = radius - (max(0, radius*radius-(radius-min(edge,radius))**2))**0.5
            self.create_line(pad + 1 + x, 61+inset, pad + 1 + x, 77-inset,
                fill=blend(self.colors[0], self.colors[1], x / max(1, right - pad - 2)))
        if filled:
            self.create_line(pad + min(7,filled/2), 64, pad + max(filled/2,filled-6), 64, fill=blend(self.colors[1], "#ffffff", .45))
        self.create_text(pad, 92, text="最近变化", anchor="w", fill="#8495af", font=self.caption_font)
        self.create_text(right, 92, text=f"参考 {self.maximum:,}", anchor="e",
                         fill="#8495af", font=self.caption_font)
