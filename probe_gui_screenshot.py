"""截图保存当前 GUI 真实外观到 probe_gui.png。"""
import os, sys
sys.path.insert(0, os.getcwd())
import tkinter as tk
import tkinter.messagebox as mb
for fn in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel"):
    setattr(mb, fn, lambda *a, **k: None)

import src.gui as gui

root = tk.Tk()
app = gui.App(root)
root.update_idletasks()
root.deiconify()
root.update()

# 选 7 层让默认值好看
app.vars["campus"].set("主校区")
app.vars["floor"].set("7层")
app.vars["room"].set("第七阅览室中区")
app.vars["seat"].set("144")
app._on_campus_change()
root.update_idletasks()
root.update()

# 取窗口的窗口 ID 截屏
try:
    import subprocess
    out = r"C:\Users\lenovo\WorkBuddy\11\ujn-seat-booker\probe_gui.png"
    # 用 powershell 把窗口画到 png
    script = (
        f"Add-Type -AssemblyName System.Drawing,System.Windows.Forms;"
        f"$b = [System.Windows.Forms.SystemInformation]::VirtualScreen;"
        f"$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height;"
        f"$g = [System.Drawing.Graphics]::FromImage($bmp);"
        f"$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size);"
        f"$bmp.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png);"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False,
                   timeout=30, capture_output=True)
    print("PNG saved:", out, "exists:", os.path.exists(out))
except Exception as e:
    print("screenshot failed:", e)

root.destroy()
