"""探查目标阅览室在某日期的座位状态，确认 001 是否空闲、有多少空闲座位。"""
from src.config import load_config
from src.session import login_browser
from src.logger import logger

LAYOUT_PRED = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"
JS_ROOT = """function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }"""
JS_FIND = """function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }"""


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        # 关闭"系统公告"等抽屉遮罩
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:
            pass
        room = cfg.get("room", "第一阅览室")
        try:
            page.get_by_text(room, exact=True).first.click(timeout=6000)
        except Exception:
            page.get_by_text(room).first.click(timeout=6000)
        page.wait_for_timeout(4000)

        make_date = cfg.get("date", "tomorrow")
        # 解析 tomorrow/today
        from datetime import date, timedelta
        if make_date == "tomorrow":
            make_date = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif make_date == "today":
            make_date = date.today().strftime("%Y-%m-%d")

        route = page.evaluate(f"""() => {{{JS_ROOT}{JS_FIND}
            const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
            const q = lp && lp.$route && lp.$route.query || {{}};
            return {{roomId: q.id, urlDate: q.makeDate}};
        }}""")
        if route.get("roomId") and route.get("urlDate") != make_date:
            url = f"https://seat.ujn.edu.cn/jsq-v/#/main/layout?id={route['roomId']}&makeDate={make_date}"
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(4500)

        # 等待 layout VM
        for _ in range(10):
            ok = page.evaluate(f"""() => {{{JS_ROOT}{JS_FIND} return !!findVM(rootVM(), {LAYOUT_PRED});}}""")
            if ok:
                break
            page.wait_for_timeout(1000)

        seats = page.evaluate(f"""() => {{{JS_ROOT}{JS_FIND}
            const vm = findVM(rootVM(), {LAYOUT_PRED});
            const objs = vm.seatPreview.design.getObjects().filter(o => o.seat);
            const out = [];
            for (const o of objs) {{
                const s = o.seat;
                const num = s.label!==undefined ? s.label : (s.id!==undefined ? s.id : (s.name!==undefined?s.name:null));
                out.push({{num:String(num), id:s.id, status:s.status}});
            }}
            return out;
        }}""")
        from collections import Counter
        cnt = Counter(s["status"] for s in seats)
        print(f"日期={make_date} 阅览室={room} 共 {len(seats)} 个座位")
        print("状态分布:", dict(cnt))
        free = [s for s in seats if s["status"] == "FREE"]
        print(f"空闲座位数={len(free)}")
        target = cfg.get("seat", "001")
        hit = [s for s in seats if str(s["num"]) == str(target) or str(s["id"]) == str(target)]
        print(f"目标座位 {target} 状态: {hit[0] if hit else '未找到'}")
        print("前 20 个空闲座位(num,id):", [(s["num"], s["id"]) for s in free[:20]])
    finally:
        sess.close()


if __name__ == "__main__":
    main()
