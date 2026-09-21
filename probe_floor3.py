"""正确探查：点击楼层下拉框、选项、过滤作用；并收集每页房间数。"""
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

    def rooms_now():
        return page.evaluate("""() => {
            const out = new Set();
            document.querySelectorAll('*').forEach(e => {
                const t=(e.textContent||'').trim();
                if(/阅览室/.test(t) && t.length<=24 && e.children.length<=2) out.add(t);
            });
            return Array.from(out).sort();
        }""")

    # 0) 各页房间一览（建立全局视图）
    print("=== 0) 默认 + 每页房间 ===")
    print("默认:", rooms_now())
    for pg in [2,3,4,5]:
        try:
            page.locator("li.number", has_text=str(pg)).first.click(timeout=4000)
            page.wait_for_timeout(2200); _close_drawer(page)
            print(f"第{pg}页:", rooms_now())
        except Exception as e:
            print(f"第{pg}页不存在或点击失败: {e}")

    # 1) 找到楼层下拉的 input/select 元素
    page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
              wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(3500); _close_drawer(page)
    print("\n=== 1) 楼层下拉的可见触发器 ===")
    triggers = page.evaluate("""() => {
        const items = [...document.querySelectorAll('.el-select-dropdown__item')]
            .filter(e => /^[2-7]层$/.test((e.textContent||'').trim()));
        if (!items.length) return 'NO DROPDOWN ITEMS YET';
        // 触发器通常是这些 dropdown item 的兄弟：.el-select 容器
        return [...document.querySelectorAll('.el-select')].map(sel => {
            const wrap = sel.closest('.selected-item-wrap, [class*="wrap"], [class*="filter"]') || sel.parentElement;
            const labels = [...wrap.querySelectorAll('*')]
                .filter(e => e.children.length===0 && (e.textContent||'').trim())
                .map(e => e.textContent.trim()).slice(0, 5);
            return {
                placeholder: sel.querySelector('.el-input__inner, input')?.value || sel.querySelector('input')?.placeholder,
                parentPreview: labels,
                outerHTMLhead: sel.outerHTML.slice(0, 200),
            };
        });
    }""")
    print(json.dumps(triggers, ensure_ascii=False, indent=2))

    # 2) 试试点击这个 select 触发器，打开 dropdown
    print("\n=== 2) 打开楼层下拉 ===")
    opened = page.evaluate("""() => {
        // 找一个含2-7层下拉项的 dropdown，向其源头 select 触发点击
        const items = [...document.querySelectorAll('.el-select-dropdown__item')]
            .filter(e => /^[2-7]层$/.test((e.textContent||'').trim()));
        if (!items.length) return 'NO ITEMS';
        // 这些 item 的最近可点击 .el-select 祖先
        let root = items[0].closest('.el-select');
        // 触发器输入
        const input = root?.querySelector('.el-input__inner, input');
        const trigger = root?.querySelector('.el-input');
        if (trigger) { trigger.click(); return 'triggered via el-input'; }
        if (input) { input.click(); return 'triggered via input'; }
        return 'NO TRIGGER';
    }""")
    print("触发:", opened)
    page.wait_for_timeout(800)

    # 3) 现在 dropdown 应可见，选 7 层
    print("\n=== 3) 选 7 层 ===")
    try:
        page.locator('.el-select-dropdown__item').filter(has_text="7层").first.click(timeout=5000)
        page.wait_for_timeout(2500); _close_drawer(page)
        print("选完7层后房间:", rooms_now())
        print("翻页器上的总页数：", page.evaluate("""() => {
            const pg = document.querySelector('.el-pagination');
            return pg ? pg.innerText.replace(/\\s+/g, ' ').slice(0, 80) : 'NO PAGINATION';
        }"""))
    except Exception as e:
        print("点7层失败:", e)

    # 4) 验证：选 7层 后第一页的房间都是 7层
    for pg in [2,3]:
        try:
            page.locator("li.number", has_text=str(pg)).first.click(timeout=4000)
            page.wait_for_timeout(2200); _close_drawer(page)
            print(f"  ... 选7层后第{pg}页房间:", rooms_now())
        except Exception as e:
            print(f"  ... 第{pg}页不存在: {e}")
finally:
    sess.close()
