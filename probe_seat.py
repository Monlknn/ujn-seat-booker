"""探查 v5：用 Vue 组件树遍历找到 page/reserve 组件，reserve(seatObj) 打开弹窗，设置时间，触发 TAC 滑块。不完成拖拽。"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import Config
from src.session import login_browser

ROOT = Path(__file__).resolve().parent
DBG = ROOT / "debug"
DBG.mkdir(exist_ok=True)

cfg = Config.load(ROOT / "config.json")
sess = login_browser(cfg, headless=False)
page = sess.page
target_seat = cfg.get("seat", "001")


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


print(">>> navigate to room")
page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
page.wait_for_timeout(3500)
close_drawer()
page.get_by_text("第一阅览室", exact=True).first.click(timeout=5000)
page.wait_for_timeout(4000)
shot("seat_01_layout")

# Vue 树遍历
print("\n>>> Vue tree: find page + reserve, open dialog")
res = page.evaluate("""(target) => {
    function rootVM(){
        const app = document.getElementById('app') || document.querySelector('#app');
        if (app && app.__vue__) return app.__vue__;
        // 退化：扫描所有元素找第一个 __vue__
        const all = document.querySelectorAll('*');
        for (const e of all) if (e.__vue__) return e.__vue__;
        return null;
    }
    function findVM(vm, pred){
        if (!vm) return null;
        if (pred(vm)) return vm;
        for (const c of (vm.$children||[])) { const r = findVM(c, pred); if (r) return r; }
        return null;
    }
    const root = rootVM();
    if (!root) return {err:'no root'};
    const pageVM = findVM(root, v => typeof v.reserve==='function');
    if (!pageVM) return {err:'no pageVM'};
    const seatPreview = pageVM.seatPreview;
    if (!seatPreview || !seatPreview.design) return {err:'no seatPreview'};
    const design = seatPreview.design;
    const norm = s => String(s).padStart(3,'0');
    let targetObj=null, targetNum=null, firstFree=null;
    for (const o of design.getObjects()) {
        if (!o.seat) continue;
        const num = o.seat.number!==undefined?o.seat.number:(o.seat.seatNumber!==undefined?o.seat.seatNumber:(o.seat.name!==undefined?o.seat.name:(o.seat.seatNo!==undefined?o.seat.seatNo:null)));
        if (num===null) continue;
        if (o.seat.status==='FREE' && !firstFree) firstFree={obj:o, num:String(num)};
        if (norm(num)===norm(target)) { targetObj=o; targetNum=String(num); }
    }
    if (!targetObj && firstFree){ targetObj=firstFree.obj; targetNum=firstFree.num; }
    if (!targetObj) return {err:'no seat'};
    pageVM.reserve(targetObj);
    return {ok:true, targetNum, seatId:targetObj.seat.id};
}""", target_seat)
print(json.dumps(res, ensure_ascii=False, indent=2))
page.wait_for_timeout(2000)
shot("seat_02_dialog")

# 找到 reserve 弹窗组件并检查
dlg = page.evaluate("""() => {
    function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
    function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
    const root=rootVM();
    const reserveVM=findVM(root, v=>typeof v.codeCheck==='function'&&typeof v.confirmFilter==='function');
    if(!reserveVM) return {open:false};
    return {open:true, show:reserveVM.show, search:reserveVM.search, startTimes:(reserveVM.startTimes||[]).slice(0,14), endTimes:(reserveVM.endTimes||[]).slice(0,14), seatNum:reserveVM.seat&&reserveVM.seat.number, seatId:reserveVM.seat&&reserveVM.seat.id};
}""")
print("  dialog:", json.dumps(dlg, ensure_ascii=False, indent=2)[:1800])
(DBG / "seat_dialog.json").write_text(json.dumps(dlg, ensure_ascii=False, indent=2), encoding="utf-8")

if not dlg.get("open"):
    print("  !! 弹窗未打开")
    sess.close(); print(">>> done"); raise SystemExit(0)

# 设置时间
print("\n>>> set time 08:00-12:00")
setres = page.evaluate("""() => {
    function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
    function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
    const reserveVM=findVM(rootVM(), v=>typeof v.codeCheck==='function'&&typeof v.confirmFilter==='function');
    const st=reserveVM.startTimes||[], et=reserveVM.endTimes||[];
    const find=(arr,want)=>{ if(arr.includes(String(want))) return String(want); const n=arr.map(Number).sort((a,b)=>Math.abs(a-want)-Math.abs(b-want)); return n.length?String(n[0]):null; };
    const sv=find(st,480), ev=find(et,720);
    if(sv) reserveVM.search.starTimeValue=sv;
    if(ev) reserveVM.search.endTimeValue=ev;
    if(typeof reserveVM.getEndTimeByStartime==='function'&&sv){ try{reserveVM.getEndTimeByStartime();}catch(e){} }
    return {setStart:sv,setEnd:ev,search:reserveVM.search};
}""")
print(json.dumps(setres, ensure_ascii=False, indent=2))
page.wait_for_timeout(600)
shot("seat_02b_timeset")

# 触发滑块
mack = page.evaluate("""() => { try { return JSON.parse(sessionStorage.getItem('systemInfo')).mackCaptcha; } catch(e){ return 'err'; } }""")
print("\n>>> mackCaptcha =", mack)
if mack:
    page.evaluate("""() => {
        function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
        function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }
        const reserveVM=findVM(rootVM(), v=>typeof v.codeCheck==='function'&&typeof v.confirmFilter==='function');
        reserveVM.codeCheck();
    }""")
    page.wait_for_timeout(3000)
    shot("seat_03_slider")
    tac = page.evaluate("""() => {
        const wrap=document.querySelector('#show-code-check-wrap');
        const res={wrapFound:!!wrap};
        if(wrap){
            res.html=wrap.outerHTML.slice(0,2800);
            res.imgs=Array.from(wrap.querySelectorAll('img')).map(i=>({alt:i.alt,cls:(i.className||'').toString().slice(0,60),w:i.naturalWidth,h:i.naturalHeight,src:(i.src||'').slice(0,60)}));
            res.divs=Array.from(wrap.querySelectorAll('div')).slice(0,50).map(d=>({cls:(d.className||'').toString().slice(0,60),w:Math.round(d.getBoundingClientRect().width),h:Math.round(d.getBoundingClientRect().height)}));
        }
        return res;
    }""")
    print("  TAC:", json.dumps(tac, ensure_ascii=False, indent=2)[:2800])
    (DBG / "seat_tac.json").write_text(json.dumps(tac, ensure_ascii=False, indent=2), encoding="utf-8")
else:
    print("  skip trigger")

sess.close()
print("\n>>> probe finished (no booking submitted)")
