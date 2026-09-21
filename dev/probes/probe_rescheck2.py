"""调试：点击记录查询后页面到底渲染了哪些文本，为什么没命中。"""
from src.config import load_config
from src.session import login_browser
from src.booker import _goto_home, _close_drawer


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        _goto_home(page)
        print("home URL:", page.url)
        # 点击记录查询
        try:
            loc = page.get_by_text("记录查询", exact=False).first
            print("记录查询 count:", loc.count())
            loc.click(timeout=4000)
            page.wait_for_timeout(3000)
            _close_drawer(page)
        except Exception as exc:
            print("点击记录查询失败:", exc)
        print("记录查询后 URL:", page.url)
        page.wait_for_timeout(1500)
        cards = page.evaluate("""() => {
            const txt = [];
            document.querySelectorAll('*').forEach(e => {
                if (e.children.length === 0) {
                    const t = (e.textContent || '').trim();
                    if (t) txt.push(t);
                }
            });
            return txt;
        }""")
        print(f"共 {len(cards)} 个叶子文本，过滤含关键词的：")
        for t in cards:
            if any(k in t for k in ("2026-08-21", "第一阅览室", "预约", "08:00", "12:00", "3075")):
                print("  >>", repr(t))
    finally:
        sess.close()


if __name__ == "__main__":
    main()
