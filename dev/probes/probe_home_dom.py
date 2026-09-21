"""探查首页楼/层/阅览室结构：楼层选择器 + 阅览室卡片 + 分页。"""
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

    # 1) 所有 select + option
    selects = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('select').forEach(s => {
            const opts = Array.from(s.options).map(o => o.text + '|' + o.value);
            out.push({id: s.id, cls: s.className, name: s.name, opts});
        });
        return out;
    }""")
    print("=== <select> 列表 ===")
    for s in selects:
        print(s)

    # 2) 含 层/楼 的可点击文本（楼层/楼栋选择器候选）
    floorish = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('*').forEach(e => {
            if (e.children.length > 0) return;
            const t = (e.textContent || '').trim();
            if (/[楼层级]/.test(t) && t.length <= 18) out.push(t);
        });
        return Array.from(new Set(out));
    }""")
    print("\n=== 含 楼/层/区 的叶子文本（候选楼层/楼栋）===")
    for f in floorish:
        print(f)

    # 3) 阅览室卡片
    rooms = page.evaluate("""() => {
        const out = new Set();
        document.querySelectorAll('*').forEach(e => {
            const t = (e.textContent || '').trim();
            if (/阅览室/.test(t) && t.length <= 24 && e.children.length <= 2) out.add(t);
        });
        return Array.from(out);
    }""")
    print("\n=== 当前可见阅览室（默认楼层/页）===")
    for r in sorted(rooms):
        print(r)
    print("共", len(rooms), "个")

    # 4) 分页控件
    pager = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('*').forEach(e => {
            const t = (e.textContent || '').trim();
            if (/下一页|上一页|第.*页|el-pager|el-pagination/.test(t) || /^\d+$/.test(t) && e.children.length===0 && t.length<=3) {
                out.push({tag: e.tagName, cls: e.className, text: t});
            }
        });
        return out.slice(0, 40);
    }""")
    print("\n=== 分页/页码元素 ===")
    for p in pager:
        print(p)

    # 5) 容器结构（rooms 所在父级）
    container = page.evaluate("""() => {
        const cards = Array.from(document.querySelectorAll('*')).filter(e => {
            const t=(e.textContent||'').trim();
            return /阅览室/.test(t) && t.length<=24 && e.children.length<=2;
        });
        if (!cards.length) return 'NO CARD';
        const parent = cards[0].parentElement;
        let chain = [];
        let el = cards[0];
        for (let i=0;i<6 && el; i++){ chain.push(el.tagName+'.'+(el.className||'').toString().slice(0,40)); el=el.parentElement; }
        return {firstCardParentTag: parent.tagName, parentCls: (parent.className||'').toString().slice(0,60),
                chain: chain};
    }""")
    print("\n=== 阅览室卡片容器结构 ===")
    print(container)

finally:
    sess.close()
