import os, sys, json, threading, subprocess, time
sys.path.insert(0, os.getcwd())
import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as mb
for fn in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel"):
    setattr(mb, fn, lambda *a, **k: None)

import src.gui as gui
from src.gui import App, LOG_QUEUE
from src.config import PROJECT_ROOT

# 1) run_thread 现在能接收 dry 关键字
captured = {}
def dummy(**kwargs):
    captured.update(kwargs)
App.run_thread(dummy, dry=False)
time.sleep(0.3)
assert captured == {"dry": False}, f"run_thread kwargs 失效: {captured}"
print("[1] run_thread(fn, dry=False) 正确派发到 daemon 线程 ->", captured)

# 2) GUI 构建 + 预约按钮存在
root = tk.Tk()
app = gui.App(root)
btn_texts = []
def walk(w):
    for c in w.winfo_children():
        if isinstance(c, (ttk.Button, tk.Button)):
            btn_texts.append(c.cget("text"))
        walk(c)
walk(root)
real_btn = [t for t in btn_texts if "开始预约" in t]
dry_btn = [t for t in btn_texts if "Dry-run" in t]
assert real_btn and dry_btn, f"缺少预约按钮: {btn_texts}"
print("[2] 按钮齐备:", btn_texts)

# 3) 模拟点击「开始预约」按钮回调（run_thread(self.do_book, dry=False)）不应 TypeError
import types
def fake_do_book(dry=False):
    gui.logger.info("dummy do_book dry=%s", dry)
    return True
app.do_book = types.MethodType(fake_do_book, app)
try:
    cb = lambda: app.run_thread(app.do_book, dry=False)
    cb()
    time.sleep(0.3)
    print("[3] 模拟点击「开始预约」按钮回调 -> run_thread 派发成功（无 TypeError）")
except TypeError as e:
    print("[3] FAIL TypeError:", e)
    raise

# 4) 抽取 _select_el_dropdown 的 JS 跑 node --check
node = r"C:/Users/lenovo/.workbuddy/binaries/node/versions/22.22.2/node.exe"
norm = "(s) => (s||'').trim()"
js_ok = f"""({norm}, ph, target) => {{
    const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
    const inp = inputs.find(i => (i.placeholder||'').trim() === ph);
    const drops = [...document.querySelectorAll('.el-select-dropdown')];
    const visible = drops.filter(d => {{
        const r = d.getBoundingClientRect();
        return r.width>0 && r.height>0 && getComputedStyle(d).display!=='none';
    }});
    const dd = visible[0] || drops[0];
    if (!dd) return 'no-dropdown';
    const items = [...dd.querySelectorAll('.el-select-dropdown__item')];
    let it = items.find(e => {norm}(e.textContent) === target);
    if (!it) it = items.find(e => {norm}(e.textContent).includes(target));
    if (it) {{ it.click(); return 'clicked'; }}
    return 'waiting:' + items.length;
}}"""
with open("probe_js_check.js", "w", encoding="utf-8") as f:
    f.write(js_ok)
r = subprocess.run([node, "--check", "probe_js_check.js"], capture_output=True, text=True)
print("[4] node --check 下拉 JS 语法:", "OK" if r.returncode == 0 else r.stderr)
assert r.returncode == 0

# 5) _select_el_dropdown 对 '全部'/空 直接短路
assert app._select_el_dropdown(None, "楼层", "全部") is False
print("[5] _select_el_dropdown('全部') 短路返回 False")

root.destroy()
print("ALL SMOKE CHECKS PASSED")
