"""探测 选了 7层 后 DOM 里有哪些阅览室卡片。"""
import sys, time
sys.path.insert(0, '.')
from src.config import load_config
from src.session import login_browser
from src.booker import _apply_home_filter, _close_drawer, _goto_home

cfg = load_config('config.json')
sess = login_browser(cfg)
page = sess.page

# 1) 不切楼层，看默认视图的阅览室
print('=== 1) 默认视图（不切楼层）所有阅览室文本 ===')
rooms = page.evaluate("""() => {
    const out = new Set();
    document.querySelectorAll('*').forEach(e => {
        const t = (e.textContent || '').trim();
        if (/阅览室/.test(t) && t.length <= 24 && e.children.length <= 2) out.add(t);
    });
    return Array.from(out);
}""")
print(f'共 {len(rooms)} 项: {sorted(rooms)}')

# 2) 切到 7层，看卡片
print()
print('=== 2) 切到 7层 后 ===')
ok = page.evaluate("""() => {
    const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
    const inp = inputs.find(i => (i.placeholder||'').trim() === '楼层');
    if (inp) { inp.click(); return true; }
    return false;
}""")
time.sleep(0.6)
clicked = page.evaluate("""() => {
    const items = [...document.querySelectorAll('.el-select-dropdown__item')];
    const it = items.find(e => (e.textContent||'').trim() === '7层');
    if (it) { it.click(); return true; }
    return false;
}""")
print(f'点击 7层 ok={clicked}')
time.sleep(2.0)
_close_drawer(page)
time.sleep(0.5)

# 再读卡片
rooms2 = page.evaluate("""() => {
    const out = new Set();
    document.querySelectorAll('*').forEach(e => {
        const t = (e.textContent || '').trim();
        if (/阅览室/.test(t) && t.length <= 24 && e.children.length <= 2) out.add(t);
    });
    return Array.from(out);
}""")
print(f'共 {len(rooms2)} 项: {sorted(rooms2)}')

# 3) 看 .el-card 数量
cards = page.evaluate("() => document.querySelectorAll('.el-card').length")
print(f'.el-card 数量: {cards}')

# 4) 看是否有分页
pagination = page.evaluate("""() => {
    const pgs = [...document.querySelectorAll('.el-pagination li')].map(e => (e.textContent||'').trim()).filter(Boolean);
    return pgs;
}""")
print(f'分页项: {pagination}')

# 5) 看每个 .el-card 的标题
titles = page.evaluate("""() => {
    return [...document.querySelectorAll('.el-card')].map(c => {
        const t = (c.textContent||'').trim().slice(0, 60);
        return t;
    });
}""")
print(f'el-card 标题:')
for t in titles:
    print(f'  - {t}')

sess.close()