"""拖动滑块并保存截图，用于验证 gap_x 与拖动映射是否正确。

流程：登录 -> 选座 -> 触发滑块 -> 用 slider.analyze 计算缺口 -> 拖动 ->
截图并读取 piece/handle 位置以及 vm.showCodeCheck，不提交 freeBook。
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import Config, load_config
from src import session as br
from src import slider as slv
from src.booker import _js_root_vm, _js_find_vm, _make_date

LAYOUT_PRED = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"
JS_DIALOG_ACTION = """
    function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
    function walkAll(vm,acc){ if(!vm)return; if(typeof vm.codeCheck==='function'&&typeof vm.confirmFilter==='function') acc.push(vm); for(const c of (vm.$children||[])) walkAll(c,acc); }
    const root=rootVM(); const arr=[]; walkAll(root,arr);
    const vm = arr.find(v=>v.show===true||v.showSelectModal===true||v.showCodeCheck===true) || arr[0] || null;
"""


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
        const track = wrap.querySelector('.bg-img-div');
        function rect(el) { if(!el)return null; const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height,left:r.left,right:r.right}; }
        return {bg:rect(bg), piece:rect(piece), handle:rect(handle), track:rect(track), wrapVisible:!!wrap.offsetParent};
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
        route = page.evaluate(f"""() => {{{_js_root_vm()}{_js_find_vm()} const lp=findVM(rootVM(), v=>v.seatPreview && v.seatPreview.design); const q=(lp&&lp.$route&&lp.$route.query)||{{}}; return {{roomId:q.id, urlDate:q.makeDate, url:location.href}};}}""")
        if route.get("roomId") and route.get("urlDate") != make_date:
            page.goto(f"https://seat.ujn.edu.cn/jsq-v/#/main/layout?id={route['roomId']}&makeDate={make_date}", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(4500)
            _close_drawer(page)

        for _ in range(10):
            if page.evaluate(f"""() => {{{_js_root_vm()}{_js_find_vm()} return !!findVM(rootVM(), {LAYOUT_PRED});}}"""):
                break
            page.wait_for_timeout(1000)

        target_seat = str(cfg.get("seat", "001"))
        pick = page.evaluate(f"""(target) => {{{_js_root_vm()}{_js_find_vm()} const vm=findVM(rootVM(), {LAYOUT_PRED}); const objs=vm.seatPreview.design.getObjects().filter(o=>o.seat); let to=null, ff=null; for(const o of objs){{ const s=o.seat; const num=s.label!==undefined?s.label:(s.id?s.id:s.name); if(s.status==='FREE'&&!ff)ff={{obj:o,num,id:s.id}}; if(target && (String(s.label)===target||String(s.id)===target||(s.name&&s.name.includes(String(target)))))to={{obj:o,num,id:s.id}}; }} if(!to&&ff)to=ff; if(!to)return {{err:'no'}}; const c=to.obj.getCenterPoint(); const vpt=vm.seatPreview.design.viewportTransform; const ix=c.x*vpt[0]+vpt[4]; const iy=c.y*vpt[1]+vpt[5]; const r=document.querySelector('#seatLayoutPreviewCanvas').getBoundingClientRect(); const scale=r.width/vm.seatPreview.design.getWidth(); return {{ok:true, px:{{x:Math.round(r.left+ix*scale), y:Math.round(r.top+iy*scale)}}}}; }}""", target_seat)
        page.mouse.click(pick["px"]["x"], pick["px"]["y"])
        page.wait_for_timeout(2500)

        def hhmm_to_min(hhmm):
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        page.evaluate(f"""(args) => {{{JS_DIALOG_ACTION} vm.search.starTimeValue=String(args.start); vm.search.endTimeValue=String(args.end); if(typeof vm.getEndTimeByStartime==='function'){{try{{vm.getEndTimeByStartime();}}catch(e){{}}}} return true;}}""", {"start": hhmm_to_min(cfg.get("start", "08:00")), "end": hhmm_to_min(cfg.get("end", "12:00"))})
        page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) vm.codeCheck(); return !!vm; }}""")
        page.wait_for_timeout(3000)

        print("拖动前:", json.dumps(_get_slider_state(page), ensure_ascii=False, indent=2))

        # 计算缺口
        res = slv.analyze(page, cfg, dump=True)
        print("analyze:", res)
        if not res:
            print("分析失败")
            return
        gap_x, nbw, npw, score, margin = res
        print(f"gap_x={gap_x} nbw={nbw} npw={npw} score={score:.1f} margin={margin:.2f}")

        # 手动执行与 solve() 相同的拖动
        track_box = slv._get_track_box(page)
        handle = slv._find_handle(page)
        if not handle:
            print("未找到 handle")
            return
        handle_loc, handle_box = handle
        displayed_track_w = track_box["width"] if track_box else handle_box["width"]
        if track_box and track_box.get("x", 0) >= handle_box.get("x", 0):
            displayed_track_w = track_box["width"]
        scale = displayed_track_w / nbw if nbw else 0.5

        # 诊断：拼图 alpha 形状左偏移量
        imgs = slv._get_tac_images(page)
        piece_b = imgs[1] if imgs else b''
        import numpy as np
        piece_arr = slv._load_image(piece_b)
        alpha = piece_arr[:, :, 3]
        ys, xs = np.where(alpha > 30)
        shape_offset_left = int(xs.min()) if len(xs) else 0
        print(f"shape_offset_left={shape_offset_left} native ({shape_offset_left*scale:.1f} display)")

        drag_dx = int(gap_x * scale)
        start_x = handle_box["x"] + handle_box["width"] / 2
        start_y = handle_box["y"] + handle_box["height"] / 2
        print(f"scale={scale:.3f} drag_dx={drag_dx} (gap_x*scale={gap_x*scale:.1f}) start=({start_x:.1f},{start_y:.1f})")

        slv._drag(page, start_x, start_y, drag_dx, cfg)

        print("拖动后:", json.dumps(_get_slider_state(page), ensure_ascii=False, indent=2))
        for t in [500, 1000, 1500, 2000, 3000]:
            page.wait_for_timeout(500)
            state = page.evaluate(f"""() => {{{JS_DIALOG_ACTION} return {{showCodeCheck: !!vm && vm.showCodeCheck, orderSuccess: !!vm && vm.orderSuccess}}; }}""")
            print(f"t+{t}ms:", state)
        page.screenshot(path=str(Path(__file__).resolve().parent / "debug" / "probe_drag_solution.png"))
        print("saved debug/probe_drag_solution.png")
    finally:
        sess.close()


if __name__ == "__main__":
    main()
