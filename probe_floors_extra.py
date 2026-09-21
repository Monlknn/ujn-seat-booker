"""探查楼层下拉是否真有 1层/8层/9层 这些候选项（不一定官方有）。"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.getcwd())
from src.config import load_config
from src.session import login_browser
from src.booker import _close_drawer

cfg = load_config()
sess = login_browser(cfg, headless=True)
page = sess.page
try:
    page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
              wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(4000); _close_drawer(page)

    # 1) 抓所有楼层下拉里出现的项
    print("=== 楼层下拉真实项 ===")
    items = page.evaluate("""() => {
        // 强制打开再读
        const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
        const inp = inputs.find(i => (i.placeholder||'').trim()==='楼层');
        if (inp) inp.click();
        return new Promise(r => setTimeout(() => {
            const all = [...document.querySelectorAll('.el-select-dropdown__item')];
            r(all.map(e => (e.textContent||'').trim()).filter(Boolean));
        }, 500));
    }""")
    if isinstance(items, dict):  # playwright can sync-wrap
        items = items
    print("  found:", items)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 2) 抓「场馆选择」下的校区
    print("\n=== 场馆选择（校区）下拉真实项 ===")
    items2 = page.evaluate("""() => {
        const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
        const inp = inputs.find(i => (i.placeholder||'').trim()==='场馆选择');
        if (inp) inp.click();
        return new Promise(r => setTimeout(() => {
            const all = [...document.querySelectorAll('.el-select-dropdown__item')];
            r(all.map(e => (e.textContent||'').trim()).filter(Boolean));
        }, 500));
    }""")
    print("  found:", items2)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 3) 看 page-level 是否有更宽的楼层清单（脚本里可能定义了）
    print("\n=== 在 window / app 状态里找楼层集合 ===")
    found = page.evaluate("""() => {
        const keys = [];
        for (const k of Object.keys(window)) {
            if (/floor/i.test(k)) keys.push(k);
        }
        return keys;
    }""")
    print("  window.*floor* keys:", found)
finally:
    sess.close()
