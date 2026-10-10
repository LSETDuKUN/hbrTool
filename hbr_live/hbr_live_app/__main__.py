import sys
import traceback
from pathlib import Path


def main():
    from .windows import set_dpi_aware
    set_dpi_aware()
    from .app import App
    app = App()
    # Explicit opt-in, useful to launch directly into connection after UAC.
    if '--connect' in sys.argv:
        app.root.after(250, app.start)
    app.root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        text = traceback.format_exc()
        path = Path(__file__).resolve().parent.parent / 'startup-error.log'
        path.write_text(text, encoding='utf-8')
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('HBR Live 启动失败', f'诊断已保存至 {path}\n\n{text[-1200:]}')
        root.destroy()
