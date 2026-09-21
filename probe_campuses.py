"""不同校区下的楼层集合是否不同。"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.getcwd())
from src.config import load_config
from src.session import login_browser
from src.booker import _close_drawer, _select_el_dropdown, _goto_home

cfg = load_config()
sess = login_browser(cfg, headless=True)
page = sess.page
try:
    for campus in ["主校区", "舜耕校区", "多功能区（主校区）"]:
        _goto_home(page)
        if campus != "主校区":
            _select_el_dropdown(page, "场馆选择", campus)
            page.wait_for_timeout(2000); _close_drawer(page)
        items = page.evaluate("""() => {
            const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
            const inp = inputs.find(i => (i.placeholder||'').trim()==='楼层');
            if (inp) inp.click();
            return new Promise(r => setTimeout(() => {
                const all = [...document.querySelectorAll('.el-select-dropdown__item')];
                const floorItems = all.filter(e => /层/.test((e.textContent||'').trim()));
                r(floorItems.map(e => (e.textContent||'').trim()).filter(Boolean));
            }, 500));
        }""")
        print(f"{campus} 楼层下拉:", items)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
finally:
    sess.close()
