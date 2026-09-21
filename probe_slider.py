"""实时滑块诊断探针（不提交预约）。

流程：登录 -> 选座 -> 打开预约弹窗 -> 设置时间 -> 触发滑块(codeCheck) ->
调用 slider.analyze(dump=True) 把实时背景/拼图存盘并求缺口偏移，
但【不拖动、不提交】，避免离线时段真实下单造成"占座未到"风险。

目的：定位此前 live 计算 gap_x=8（应为~223）的根因——到底是提取错了图片，
还是匹配本身在真实图上有问题。
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import Config, load_config
from src import session as br
from src import slider as slv

# 复用 booker 里的 JS 片段
from src.booker import _js_root_vm, _js_find_vm, _make_date

LAYOUT_PRED = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"


def main():
    cfg = load_config()
    print("config:", json.dumps(cfg.as_dict(), ensure_ascii=False)[:400])
    sess = br.login_browser(cfg)
    try:
        page = sess.page
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        _close_drawer(page)

        room_name = cfg.get("room", "第一阅览室")
        try:
            page.get_by_text(room_name, exact=True).first.click(timeout=6000)
        except Exception as exc:
            print("阅览室精确点击失败，改用包含匹配:", exc)
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
        print("route:", route)
        if route.get("roomId") and route.get("urlDate") != make_date:
            url = (f"https://seat.ujn.edu.cn/jsq-v/#/main/layout?"
                   f"id={route['roomId']}&makeDate={make_date}")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(4500)
            _close_drawer(page)

        # 等待 layout VM
        for _ in range(10):
            ok = page.evaluate(f"""() => {{
                {_js_root_vm()}
                {_js_find_vm()}
                return !!findVM(rootVM(), {LAYOUT_PRED});
            }}""")
            if ok:
                break
            page.wait_for_timeout(1000)
        else:
            print("布局页未加载")
            return

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
            if (!targetObj) return {{err:'no seat', count:objs.length}};
            const o = targetObj.obj;
            const c = o.getCenterPoint();
            const vpt = vm.seatPreview.design.viewportTransform;
            const zoom = vpt[0];
            const ix = c.x*zoom + vpt[4];
            const iy = c.y*zoom + vpt[5];
            const cvs = document.querySelector('#seatLayoutPreviewCanvas');
            const r = cvs.getBoundingClientRect();
            const scale = r.width / vm.seatPreview.design.getWidth();
            const sx = r.left + ix*scale;
            const sy = r.top + iy*scale;
            return {{ok:true, chosen:targetObj.num, seatId:String(targetObj.id),
                    px:{{x:Math.round(sx), y:Math.round(sy)}}}};
        }}""", target_seat)
        print("pick:", pick)
        if pick.get("err"):
            print("无可选座位")
            return
        page.mouse.click(pick["px"]["x"], pick["px"]["y"])
        page.wait_for_timeout(2500)

        JS_DIALOG_ACTION = """
            function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
            function walkAll(vm,acc){ if(!vm)return; if(typeof vm.codeCheck==='function'&&typeof vm.confirmFilter==='function') acc.push(vm); for(const c of (vm.$children||[])) walkAll(c,acc); }
            const root=rootVM(); const arr=[]; walkAll(root,arr);
            const vm = arr.find(v=>v.show===true||v.showSelectModal===true||v.showCodeCheck===true) || arr[0] || null;
        """

        # 等待弹窗
        for _ in range(8):
            if page.evaluate(f"""() => {{{JS_DIALOG_ACTION} return !!vm; }}"""):
                break
            page.wait_for_timeout(500)

        # 设置时间
        def hhmm_to_min(hhmm):
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        start_min = hhmm_to_min(cfg.get("start", "08:00"))
        end_min = hhmm_to_min(cfg.get("end", "12:00"))
        page.evaluate(f"""(args) => {{
            {JS_DIALOG_ACTION}
            if (!vm) return false;
            vm.search.starTimeValue = String(args.start);
            vm.search.endTimeValue = String(args.end);
            if (typeof vm.getEndTimeByStartime === 'function') {{ try {{ vm.getEndTimeByStartime(); }} catch(e){{}} }}
            return true;
        }}""", {"start": start_min, "end": end_min})
        print("时间已设置", start_min, end_min)

        mack = page.evaluate("""() => {
            try {
                const si = (typeof sessionStorageProxy!=='undefined' && sessionStorageProxy) ? sessionStorageProxy.getItem('systemInfo') : sessionStorage.getItem('systemInfo');
                if (!si) return null;
                return JSON.parse(si).mackCaptcha;
            } catch(e){ return null; }
        }""")
        print("mackCaptcha=", mack)

        if not mack:
            print("无需滑块")
            return

        page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) vm.codeCheck(); return !!vm; }}""")
        page.wait_for_timeout(3000)

        wrap_ok = page.locator("#show-code-check-wrap").first.is_visible(timeout=3000)
        print("滑块 wrap 可见:", wrap_ok)

        # 关键：提取 + 求缺口，但【不拖动】
        res = slv.analyze(page, cfg, dump=True)
        print("analyze 结果:", res)
        if res:
            gap_x, nbw, npw, score, margin = res
            print(f"gap_x={gap_x}  native_bg_w={nbw}  fraction={gap_x/nbw if nbw else None}  score={score:.1f}  margin={margin:.2f}")
        print("已保存调试图到 debug/tac_live_*.png 与 tac_live_dom.json；未拖动、未提交。")
        page.screenshot(path=str(Path(__file__).resolve().parent / "debug" / "probe_slider_final.png"))
    finally:
        sess.close()


def _close_drawer(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:
        pass


if __name__ == "__main__":
    main()
