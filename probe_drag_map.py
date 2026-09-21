"""测量 TAC 滑块手柄拖动距离与拼图移动距离的映射关系。

流程：登录 -> 选座 -> 打开预约弹窗 -> 触发滑块 ->
记录初始位置 -> 手动拖动一段固定距离 -> 记录结束位置，
计算 handle / piece 分别移动了多少像素，从而修正 solve() 中的拖动距离。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import Config, load_config
from src import session as br
from src.booker import _js_root_vm, _js_find_vm, _make_date

LAYOUT_PRED = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"


def _close_drawer(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:
        pass


def _get_slider_state(page):
    return page.evaluate("""() => {
        const wrap = document.querySelector('#show-code-check-wrap');
        if (!wrap) return null;
        const bg = wrap.querySelector('#tianai-captcha-slider-bg-img');
        const piece = wrap.querySelector('#tianai-captcha-slider-move-img') || wrap.querySelector('.slider-img-div img');
        const handle = wrap.querySelector('.slider-move-btn');
        const pieceDiv = wrap.querySelector('.slider-img-div');
        const track = wrap.querySelector('.bg-img-div');
        function rect(el) {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height, left: r.left, top: r.top, right: r.right, bottom: r.bottom};
        }
        function style(el, k) { return el ? (getComputedStyle(el)[k] || '') : ''; }
        return {
            bg: rect(bg), piece: rect(piece), handle: rect(handle),
            pieceDiv: rect(pieceDiv), track: rect(track),
            pieceDivStyle: {left: style(pieceDiv, 'left'), top: style(pieceDiv, 'top'), transform: style(pieceDiv, 'transform')},
            handleStyle: {left: style(handle, 'left'), top: style(handle, 'top'), transform: style(handle, 'transform')},
        };
    }""")


def main():
    cfg = load_config()
    sess = br.login_browser(cfg)
    try:
        page = sess.page
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        _close_drawer(page)
        room_name = cfg.get("room", "第一阅览室")
        try:
            page.get_by_text(room_name, exact=True).first.click(timeout=6000)
        except Exception:
            page.get_by_text(room_name).first.click(timeout=6000)
        page.wait_for_timeout(5000)
        _close_drawer(page)

        make_date = _make_date(cfg.get("date", "tomorrow"))
        route = page.evaluate(f"""() => {{
            {_js_root_vm()}
            {_js_find_vm()}
            const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
            const q = lp && lp.$route && lp.$route.query || {{}};
            return {{roomId: q.id, urlDate: q.makeDate, url: location.href}};
        }}""")
        if route.get("roomId") and route.get("urlDate") != make_date:
            page.goto(f"https://seat.ujn.edu.cn/jsq-v/#/main/layout?id={route['roomId']}&makeDate={make_date}",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(4500)
            _close_drawer(page)

        for _ in range(10):
            if page.evaluate(f"""() => {{{_js_root_vm()}{_js_find_vm()} return !!findVM(rootVM(), {LAYOUT_PRED});}}"""):
                break
            page.wait_for_timeout(1000)

        target_seat = str(cfg.get("seat", "001"))
        pick = page.evaluate(f"""(target) => {{
            {_js_root_vm()}
            {_js_find_vm()}
            const vm = findVM(rootVM(), {LAYOUT_PRED});
            const objs = vm.seatPreview.design.getObjects().filter(o => o.seat);
            let targetObj=null, firstFree=null;
            for (const o of objs){{
                const s = o.seat;
                const num = s.label!==undefined ? s.label : (s.id!==undefined ? s.id : (s.name!==undefined?s.name:null));
                if (s.status==='FREE' && !firstFree) firstFree={{obj:o, num:String(num), id:s.id}};
                if (target && (String(s.label)===String(target) || String(s.id)===String(target) ||
                               (s.name && s.name.includes(String(target))))) targetObj={{obj:o, num:String(num), id:s.id}};
            }}
            if (!targetObj && firstFree) targetObj=firstFree;
            if (!targetObj) return {{err:'no seat'}};
            const o = targetObj.obj;
            const c = o.getCenterPoint();
            const vpt = vm.seatPreview.design.viewportTransform;
            const zoom = vpt[0];
            const ix = c.x*zoom + vpt[4];
            const iy = c.y*zoom + vpt[5];
            const cvs = document.querySelector('#seatLayoutPreviewCanvas');
            const r = cvs.getBoundingClientRect();
            const scale = r.width / vm.seatPreview.design.getWidth();
            return {{ok:true, px:{{x:Math.round(r.left + ix*scale), y:Math.round(r.top + iy*scale)}}}};
        }}""", target_seat)
        page.mouse.click(pick["px"]["x"], pick["px"]["y"])
        page.wait_for_timeout(2500)

        JS_DIALOG_ACTION = """
            function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
            function walkAll(vm,acc){ if(!vm)return; if(typeof vm.codeCheck==='function'&&typeof vm.confirmFilter==='function') acc.push(vm); for(const c of (vm.$children||[])) walkAll(c,acc); }
            const root=rootVM(); const arr=[]; walkAll(root,arr);
            const vm = arr.find(v=>v.show===true||v.showSelectModal===true||v.showCodeCheck===true) || arr[0] || null;
        """
        for _ in range(8):
            if page.evaluate(f"""() => {{{JS_DIALOG_ACTION} return !!vm; }}"""):
                break
            page.wait_for_timeout(500)

        def hhmm_to_min(hhmm):
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        page.evaluate(f"""(args) => {{
            {JS_DIALOG_ACTION}
            vm.search.starTimeValue = String(args.start);
            vm.search.endTimeValue = String(args.end);
            if (typeof vm.getEndTimeByStartime === 'function') {{ try {{ vm.getEndTimeByStartime(); }} catch(e){{}} }}
            return true;
        }}""", {"start": hhmm_to_min(cfg.get("start", "08:00")), "end": hhmm_to_min(cfg.get("end", "12:00"))})
        page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) vm.codeCheck(); return !!vm; }}""")
        page.wait_for_timeout(3000)

        state1 = _get_slider_state(page)
        print("初始 state:", state1)

        handle = page.locator("#show-code-check-wrap .slider-move-btn").first
        box = handle.bounding_box()
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        drag_dx = 80  # 拖动 80 像素做标定

        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x + drag_dx, start_y)
        page.mouse.up()
        page.wait_for_timeout(800)

        state2 = _get_slider_state(page)
        print("拖动后 state:", state2)

        print(f"鼠标拖动 delta_x={drag_dx}")
        if state1 and state2:
            h1 = state1["handle"]; h2 = state2["handle"]
            p1 = state1["piece"]; p2 = state2["piece"]
            if h1 and h2:
                print(f"handle center 移动: {h2['x'] + h2['w']/2 - (h1['x'] + h1['w']/2):.1f}")
                print(f"handle left 移动: {h2['left'] - h1['left']:.1f}")
            if p1 and p2:
                print(f"piece left 移动: {p2['left'] - p1['left']:.1f}")
                print(f"piece img left 移动: {p2['x'] - p1['x']:.1f}")
        page.screenshot(path=str(Path(__file__).resolve().parent / "debug" / "probe_drag_map.png"))
    finally:
        sess.close()


if __name__ == "__main__":
    main()
