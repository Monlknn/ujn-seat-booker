"""Live probe (no booking submitted): open reserve dialog via real canvas click,
then render the TAC slider to capture its DOM + drag-handle selector.

Flow:
  login -> close drawer -> home -> click room text -> reach layout
  -> wait for canvas + find layout VM (seatPreview.design)
  -> pick a FREE seat, compute its on-screen pixel, click it
  -> verify seat set, find mounted reserve dialog VM
  -> read mackCaptcha; if truthy, call codeCheck() to render #show-code-check-wrap
  -> capture slider DOM + handle selector + screenshot.  NO drag, NO submit.
"""
import sys
import json
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config
from src.session import login_browser

ROOT = Path(__file__).resolve().parents[2]
DBG = ROOT / "debug"
DBG.mkdir(exist_ok=True)
cfg = Config.load(ROOT / "config.json")
target_seat = str(cfg.get("seat", "001"))

sess = login_browser(cfg, headless=cfg.get("headless", True))
page = sess.page


def shot(name):
    try:
        page.screenshot(path=str(DBG / f"{name}.png"))
        print(f"[shot] {name}.png")
    except Exception as exc:
        print("  shot err:", exc)


def close_drawer():
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:
        pass


def _find_js(pred_js):
    return f"""() => {{
        function rootVM(){{ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }}
        function findVM(vm,pred){{ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){{const r=findVM(c,pred); if(r)return r;}} return null; }}
        return findVM(rootVM(), {pred_js});
    }}"""


def _has_js(pred_js):
    return f"""() => {{
        function rootVM(){{ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }}
        function findVM(vm,pred){{ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){{const r=findVM(c,pred); if(r)return r;}} return null; }}
        return !!findVM(rootVM(), {pred_js});
    }}"""


print(">>> login done; URL=", page.url)
page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
page.wait_for_timeout(3500)
close_drawer()

# ---- click room text to reach layout (proven path) ----
print(">>> click room:", cfg.get("room"))
try:
    page.get_by_text(cfg.get("room"), exact=True).first.click(timeout=6000)
except Exception as exc:
    print("  room click err:", exc)
page.wait_for_timeout(4500)
close_drawer()
shot("probe_layout")

# ---- wait for canvas / layout VM ----
layout_pred = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"
for attempt in range(8):
    lp_ready = page.evaluate(_has_js(layout_pred))
    print(f"  layout VM ready? {lp_ready} (attempt {attempt+1})")
    if lp_ready:
        break
    page.wait_for_timeout(1000)
if not lp_ready:
    print("!! layout VM never appeared")
    sess.close()
    raise SystemExit(1)

# ---- pick target seat from fabric objects ----
pick = page.evaluate("""(target) => {
    function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
    function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
    const vm = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function');
    const objs = vm.seatPreview.design.getObjects().filter(o => o.seat);
    let targetObj=null, firstFree=null, labels=[];
    for (const o of objs){
        const s = o.seat;
        const num = s.label!==undefined ? s.label : (s.id!==undefined ? s.id : (s.name!==undefined?s.name:null));
        if (s.label) labels.push(String(s.label));
        if (s.status==='FREE' && !firstFree) firstFree={obj:o, num:String(num), id:s.id};
        if (target && (String(s.label)===String(target) || String(s.id)===String(target) ||
                       (s.name && s.name.includes(String(target))))) targetObj={obj:o, num:String(num), id:s.id};
    }
    if (!targetObj && firstFree) targetObj=firstFree;
    if (!targetObj) return {err:'no seat', labels:labels.slice(0,20), count:objs.length};
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
    return {ok:true, chosen:targetObj.num, seatId:String(targetObj.id),
            labels:labels.slice(0,20), count:objs.length,
            px:{x:Math.round(sx), y:Math.round(sy)},
            info:{zoom, vpt, canvasW:vm.seatPreview.design.getWidth(), rectW:Math.round(r.width),
                  rectLeft:Math.round(r.left), rectTop:Math.round(r.top)}};
}""", target_seat)
print("\n>>> seat pick:", json.dumps(pick, ensure_ascii=False, indent=2)[:1600])
(DBG / "probe_pick.json").write_text(json.dumps(pick, ensure_ascii=False, indent=2), encoding="utf-8")
if pick.get("err"):
    sess.close()
    raise SystemExit(1)

# ---- real canvas click ----
px = pick["px"]
print(f"\n>>> click canvas at ({px['x']}, {px['y']})  [chosen seat {pick['chosen']} id {pick['seatId']}]")
page.mouse.click(px["x"], px["y"])
page.wait_for_timeout(2500)
shot("probe_after_click")

state = page.evaluate("""() => {
    function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
    function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
    const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
    const seat = lp.seat || {};
    return {seatId: seat.id, seatSeatId: seat.seat && seat.seat.id, seatLabel: seat.seat && seat.seat.label,
            seatName: seat.seat && seat.seat.name,
            showSelectModal: lp.showSelectModal, show: lp.show, showCodeCheck: lp.showCodeCheck,
            dialogOpen: (typeof lp.showSelectModal!=='undefined' && lp.showSelectModal) || (typeof lp.show!=='undefined' && lp.show)};
}""")
print("\n>>> after-click state:", json.dumps(state, ensure_ascii=False, indent=2))
(DBG / "probe_clickstate.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# fallback: if seat not set, force open via setting lp.seat + showSelectModal
if not state.get("dialogOpen"):
    print(">>> click didn't open modal, fallback: set seat + showSelectModal on layout VM")
    page.evaluate("""(seatId) => {
        function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
        function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
        const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
        const objs = lp.seatPreview.design.getObjects().filter(o=>o.seat);
        const target = objs.find(o=>String(o.seat.id)===seatId) || objs.find(o=>o.seat.status==='FREE');
        if (target){ lp.seat=target; lp.showSelectModal=true; }
    }""", pick["seatId"])
    page.wait_for_timeout(2000)
    shot("probe_after_fallback")
    state = page.evaluate("""() => {
        function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
        function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
        const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
        return {dialogOpen: (typeof lp.showSelectModal!=='undefined' && lp.showSelectModal) || (typeof lp.show!=='undefined' && lp.show),
                showSelectModal: lp.showSelectModal, show: lp.show};
    }""")
    print("  after fallback:", json.dumps(state, ensure_ascii=False, indent=2))
    if not state.get("dialogOpen"):
        print("!! still no dialog")
        sess.close()
        raise SystemExit(1)

# ---- read mackCaptcha ----
mack = page.evaluate("""() => {
    try {
        const si = (typeof sessionStorageProxy!=='undefined' && sessionStorageProxy) ? sessionStorageProxy.getItem('systemInfo') : sessionStorage.getItem('systemInfo');
        if (!si) return 'no-systemInfo';
        const obj = JSON.parse(si);
        return obj.mackCaptcha;
    } catch(e){ return 'err:'+e; }
}""")
print("\n>>> mackCaptcha =", mack)

# ---- if captcha enabled, render slider (NO drag) ----
if mack:
    print(">>> captcha ENABLED -> render TAC slider (no drag)")
    page.evaluate("""() => {
        function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
        function walkAll(vm,acc){ if(!vm)return; if(typeof vm.codeCheck==='function'&&typeof vm.confirmFilter==='function') acc.push(vm); for(const c of (vm.$children||[])) walkAll(c,acc); }
        const root=rootVM(); const arr=[]; walkAll(root,arr);
        let t = arr.find(v=>v.show===true||v.showSelectModal===true||v.showCodeCheck===true) || arr[0];
        if (t) t.codeCheck();
    }""")
    page.wait_for_timeout(3500)
    shot("probe_slider")
    tac = page.evaluate("""() => {
        const wrap=document.querySelector('#show-code-check-wrap');
        const res={wrapFound:!!wrap};
        if(wrap){
            res.html=wrap.outerHTML.slice(0,2600);
            res.imgs=Array.from(wrap.querySelectorAll('img')).map(i=>({alt:i.alt,cls:(i.className||'').toString().slice(0,60),w:i.naturalWidth,h:i.naturalHeight,src:(i.src||'').slice(0,50)}));
            const handleCands=[];
            Array.from(wrap.querySelectorAll('div')).forEach(d=>{
                const cls=(d.className||'').toString();
                const rect=d.getBoundingClientRect();
                if(rect.width>0 && rect.width<rect.height*5 && rect.height>0){
                    handleCands.push({cls:cls.slice(0,60), w:Math.round(rect.width), h:Math.round(rect.height), x:Math.round(rect.x), y:Math.round(rect.y)});
                }
            });
            res.handleCands=handleCands.slice(0,40);
        }
        return res;
    }""")
    print("\n>>> TAC slider DOM:", json.dumps(tac, ensure_ascii=False, indent=2)[:3000])
    (DBG / "probe_tac.json").write_text(json.dumps(tac, ensure_ascii=False, indent=2), encoding="utf-8")
else:
    print(">>> captcha DISABLED for this account (mackCaptcha falsy) -> would book directly via codeCheck; skipping to avoid real booking in probe.")

sess.close()
print("\n>>> probe finished (NO booking submitted)")
