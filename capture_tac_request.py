"""触发滑块并捕获 gen/SLIDER 请求，查看真实 URL 与请求头。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.config import load_config
from src import session as br
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


def main():
    cfg = load_config()
    sess = br.login_browser(cfg)
    try:
        page = sess.page
        requests_log = []
        def on_req(req):
            url = req.url
            if "cap/cg/gen" in url or "gen/SLIDER" in url or "validCaptcha" in url:
                print("\n[REQUEST]", url)
                print("  headers:", dict(req.headers))
                print("  postData:", (req.post_data_json or req.post_data)[:200] if req.post_data else None)
                requests_log.append({"url": url, "headers": dict(req.headers), "post": req.post_data})
        page.on("request", on_req)

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
        pick = page.evaluate(f"""(target) => {{{_js_root_vm()}{_js_find_vm()} const vm=findVM(rootVM(), {LAYOUT_PRED}); const objs=vm.seatPreview.design.getObjects().filter(o=>o.seat); let to=null, ff=null; for(const o of objs){{ const s=o.seat; const num=s.label!==undefined?s.label:(s.id?s.id:s.name); if(s.status==='FREE'&&!ff)ff={{obj:o, num, id:s.id}}; if(target && (String(s.label)===target||String(s.id)===target||(s.name&&s.name.includes(String(target)))))to={{obj:o, num, id:s.id}}; }} if(!to&&ff)to=ff; if(!to)return {{err:'no'}}; const c=to.obj.getCenterPoint(); const vpt=vm.seatPreview.design.viewportTransform; const ix=c.x*vpt[0]+vpt[4]; const iy=c.y*vpt[1]+vpt[5]; const r=document.querySelector('#seatLayoutPreviewCanvas').getBoundingClientRect(); const scale=r.width/vm.seatPreview.design.getWidth(); return {{ok:true, px:{{x:Math.round(r.left+ix*scale), y:Math.round(r.top+iy*scale)}}}}; }}""", target_seat)
        page.mouse.click(pick["px"]["x"], pick["px"]["y"])
        page.wait_for_timeout(2500)

        def hhmm_to_min(hhmm):
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        page.evaluate(f"""(args) => {{{JS_DIALOG_ACTION} vm.search.starTimeValue=String(args.start); vm.search.endTimeValue=String(args.end); if(typeof vm.getEndTimeByStartime==='function'){{try{{vm.getEndTimeByStartime();}}catch(e){{}}}} return true;}}""", {"start": hhmm_to_min(cfg.get("start", "08:00")), "end": hhmm_to_min(cfg.get("end", "12:00"))})
        page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) vm.codeCheck(); return !!vm; }}""")
        page.wait_for_timeout(4000)
        print("\n共捕获相关请求:", len(requests_log))
        Path("debug/tac_requests.json").write_text(json.dumps(requests_log, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved to debug/tac_requests.json")
    finally:
        sess.close()


if __name__ == "__main__":
    main()
