"""抓取当前账号的现有预约，定位 freeBook 报 '所选时段已有有效预约' 的冲突来源。"""
from src.config import load_config
from src.session import login_browser

JS_ROOT = """function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }"""


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        captured = []

        def _on_resp(response):
            u = response.url
            if any(k in u for k in ("reserv", "order", "record", "myReserv", "current", "frontApi")):
                try:
                    body = response.json()
                except Exception:
                    try:
                        body = response.text()
                    except Exception:
                        body = None
                captured.append((u, response.status, body))

        page.on("response", _on_resp)

        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(4000)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:
            pass

        # 1) 列出首页可见导航文字，定位"我的预约"入口
        navs = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('a,button,[role=menuitem],.el-menu-item,[class*=menu]'))
                .map(e => (e.textContent||'').trim()).filter(t => t && t.length <= 12);
            return [...new Set(els)];
        }""")
        print("首页可见导航/按钮:", navs)

        # 2) 尝试点击可能的"我的/预约记录"入口并读取列表
        for kw in ("我的预约", "我的", "预约记录", "当前预约", "记录"):
            try:
                loc = page.get_by_text(kw, exact=False).first
                if loc.count():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

        # 3) 读取页面上所有疑似预约卡片/行的文本
        rows = page.evaluate("""() => {
            const txt = [];
            document.querySelectorAll('*').forEach(e => {
                if (e.children.length === 0 || e.children.length === 1) {
                    const t = (e.textContent||'').trim();
                    if (t && /(预约|座位|阅览室|签到|有效期|08:|09:|10:|11:|12:|2026|失效|违约)/.test(t) && t.length < 120) txt.push(t);
                }
            });
            return [...new Set(txt)].slice(0, 40);
        }""")
        print("页面预约相关文本:", rows)

        print("---- 捕获的相关 API 响应 ----")
        for u, st, body in captured:
            print(f"[{st}] {u}")
            print("   ", body)
    finally:
        sess.close()


if __name__ == "__main__":
    main()
