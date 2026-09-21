"""深入探查楼层 tab：JS 触发点击 + 校验 DOM 反应 + 看房间底层的所有 dataset。"""
from __future__ import annotations
import sys, os, json
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

    # 1) 找楼层 tab 元素 + 它们在父容器里的位置（看是否 hidden）
    print("=== 楼层 tab 元素 box ===")
    print(page.evaluate("""() => {
        const tabs = [...document.querySelectorAll('span')]
            .filter(e => (e.textContent||'').trim().match(/^[2-7]层$/));
        return tabs.map(e => {
            const r = e.getBoundingClientRect();
            const cs = getComputedStyle(e);
            return {
                text: e.textContent.trim(),
                visible: r.width>0 && r.height>0 && cs.display!=='none' && cs.visibility!=='hidden',
                rect: {x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)},
                display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
                parentCls: (e.parentElement?.className||'').toString().slice(0,80),
            };
        });
    }"""))

    # 2) 抽一个房间卡片的 outerHTML（看真实 selector/DOM 结构）
    print("\n=== 一个房间卡片的 outerHTML（截取前1200字符） ===")
    print(page.evaluate("""() => {
        const target = [...document.querySelectorAll('*')]
            .find(e => (e.textContent||'').trim()==='第七阅览室中区' && e.children.length===0);
        let card = target;
        for (let i=0; i<6 && card; i++) {
            const p = card.parentElement;
            if (p && p.children.length >= 2 && p !== document.body) card = p;
            else break;
        }
        return card ? card.outerHTML.slice(0, 1200) : 'NOT FOUND';
    }"""))

    # 3) 翻第2页后看楼层 tab 是否变化（排除"楼层 tab 跟页面无关"）
    print("\n=== 翻第 2 页 ===")
    try:
        page.locator("li.number", has_text="2").first.click(timeout=5000)
        page.wait_for_timeout(2500)
        _close_drawer(page)
        print("已翻到第2页；楼层 tab 状态：")
        print(page.evaluate("""() => {
            const tabs = [...document.querySelectorAll('span')]
                .filter(e => (e.textContent||'').trim().match(/^[2-7]层$/));
            const parent = tabs[0]?.parentElement;
            return {
                tabTexts: tabs.map(e => e.textContent.trim()),
                parentClassList: parent ? [...parent.classList] : null,
                activeText: parent?.querySelector('.active, .selected, [class*="active"], [class*="selected"]')?.textContent?.trim() || null,
            };
        }"""))
    except Exception as e:
        print("翻页失败:", e)
finally:
    sess.close()
