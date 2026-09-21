"""探查：点击楼层 tab 后房间列表如何变化；以及分页里有哪些房间。"""
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
    page.wait_for_timeout(4000)
    _close_drawer(page)

    def rooms_now():
        return page.evaluate("""() => {
            const out = new Set();
            document.querySelectorAll('*').forEach(e => {
                const t=(e.textContent||'').trim();
                if(/阅览室/.test(t) && t.length<=24 && e.children.length<=2) out.add(t);
            });
            return Array.from(out).sort();
        }""")

    def floor_tab_info():
        return page.evaluate("""() => {
            // 找到“楼层”标签，再看它兄弟/父容器里的可点击项
            const all=[...document.querySelectorAll('*')];
            const label=all.find(e=> (e.textContent||'').trim()==='楼层' && e.children.length===0);
            let ctx = label ? label.parentElement : null;
            const items=[];
            if(ctx){ ctx.querySelectorAll('*').forEach(e=>{
                const t=(e.textContent||'').trim();
                if(/^[2-7]层$/.test(t) && e.children.length===0) items.push({tag:e.tagName, cls:e.className, text:t});
            });}
            return {parentCls: ctx?(ctx.className||'').toString().slice(0,80):'NONE', items};
        }""")

    print("=== 楼层 tab 容器 ===")
    print(floor_tab_info())

    print("\n=== 默认(未选楼层)房间 ===")
    print(rooms_now())

    # 点击 7层
    print("\n--- 点击 7层 ---")
    try:
        page.get_by_text("7层", exact=True).first.click(timeout=6000)
        page.wait_for_timeout(3000)
        _close_drawer(page)
        print("7层 房间:", rooms_now())
        print("7层 tab 容器:", floor_tab_info())
    except Exception as e:
        print("点击7层失败:", e)

    # 回到默认，试试翻到第2/3页看是否有 第七阅览室
    print("\n--- 默认视图翻页探查 ---")
    page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(3500)
    _close_drawer(page)
    for pg in [2,3]:
        try:
            page.locator("li.number", has_text=str(pg)).first.click(timeout=5000)
            page.wait_for_timeout(2500)
            _close_drawer(page)
            print(f"第{pg}页房间:", rooms_now())
        except Exception as e:
            print(f"翻第{pg}页失败:", e)
finally:
    sess.close()
