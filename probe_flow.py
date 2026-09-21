"""探查：fabric 画布座位对象、预约弹窗结构、TAC 滑块 DOM（不完成拖拽，避免误预约）。"""
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


# 1) 进入布局页
print(">>> open SPA + navigate to room")
page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
page.wait_for_timeout(3500)
close_drawer()
try:
    page.get_by_text("第一阅览室", exact=True).first.click(timeout=5000)
    page.wait_for_timeout(3500)
except Exception as exc:
    print("  room click:", exc)
print("URL:", page.url)
shot("flow_01_layout")

# 2) 探查 fabric 座位对象
print("\n>>> probe fabric seats")
info = page.evaluate("""() => {
    const res = {canvasFound:false, fabricFound:false, seats:[], sampleProps:null, canvasRect:null};
    const cvs = document.querySelector('.seatlayoutcanvascontainer canvas');
    if (!cvs) return res;
    res.canvasFound = true;
    const r = cvs.getBoundingClientRect();
    res.canvasRect = {x:r.x, y:r.y, w:r.width, h:r.height, cw:cvs.width, ch:cvs.height};
    // 尝试多种 fabric 实例获取方式
    let fc = cvs._fabric; // fabric 1.x
    if (!fc && cvs.__fabric) fc = cvs.__fabric;
    // 通过 Vue 组件查找
    if (!fc) {
        const walk = (el) => {
            if (fc) return;
            if (el.__vue__) {
                const v = el.__vue__;
                const cand = [v.seatPreview, v.seatCanvas, v.canvas, v.fabricCanvas];
                for (const c of cand) { if (c && c.getObjects) { fc = c; return; } }
                // 递归子组件
                const children = v.$children || [];
                for (const ch of children) { if (ch && ch.getObjects) { fc = ch; return; } }
            }
            for (const c of el.children) walk(c);
        };
        walk(document.body);
    }
    res.fabricFound = !!fc;
    if (fc && fc.getObjects) {
        const objs = fc.getObjects();
        res.objCount = objs.length;
        if (objs[0]) res.sampleProps = Object.keys(objs[0]).filter(k=>!k.startsWith('_')).slice(0,40);
        for (const o of objs) {
            const num = o.seatNumber !== undefined ? o.seatNumber : (o.number !== undefined ? o.number : (o.seatId || o.id));
            if (num === undefined) continue;
            let rect = null;
            try { const b = o.getBoundingRect(true); rect = {x:Math.round(b.left), y:Math.round(b.top), w:Math.round(b.width), h:Math.round(b.height)}; } catch(e){}
            res.seats.push({num:String(num), id:o.id, status:o.status, rect});
        }
    }
    return res;
}""")
print("  canvasFound:", info.get("canvasFound"), "fabricFound:", info.get("fabricFound"))
print("  canvasRect:", info.get("canvasRect"))
print("  objCount:", info.get("objCount"))
print("  sampleProps:", info.get("sampleProps"))
seats = info.get("seats", [])
print("  seat-like objects:", len(seats))
# 找目标座位
target = None
for s in seats:
    if s["num"] == target_seat or s["num"].zfill(3) == target_seat.zfill(3):
        target = s
        break
if target:
    print(f"  TARGET seat {target_seat} ->", target)
    if target.get("rect"):
        cr = info["canvasRect"]
        sx = cr["x"] + target["rect"]["x"] + target["rect"]["w"]/2
        sy = cr["y"] + target["rect"]["y"] + target["rect"]["h"]/2
        print(f"  -> screen pixel ~ ({sx:.0f}, {sy:.0f})")
        info["_target_screen"] = {"x": sx, "y": sy}
# 保存
(DBG / "flow_fabric.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

# 3) 点击目标座位
print("\n>>> click target seat")
clicked = False
if target and target.get("rect"):
    cr = info["canvasRect"]
    sx = cr["x"] + target["rect"]["x"] + target["rect"]["w"]/2
    sy = cr["y"] + target["rect"]["y"] + target["rect"]["h"]/2
    print(f"  clicking canvas at ({sx:.0f},{sy:.0f})")
    try:
        page.mouse.click(sx, sy)
        page.wait_for_timeout(2500)
        clicked = True
    except Exception as exc:
        print("  click err:", exc)
elif seats:
    # 退而求其次：点第一个可用座位（rect 存在）
    for s in seats:
        if s.get("rect") and (s.get("status") in (0, "0", None, "free", "available")):
            cr = info["canvasRect"]
            sx = cr["x"] + s["rect"]["x"] + s["rect"]["w"]/2
            sy = cr["y"] + s["rect"]["y"] + s["rect"]["h"]/2
            print(f"  clicking first free-ish seat {s['num']} at ({sx:.0f},{sy:.0f})")
            try:
                page.mouse.click(sx, sy)
                page.wait_for_timeout(2500)
                clicked = True
                target = s
                break
            except Exception as exc:
                print("  click err:", exc)
shot("flow_02_dialog")
if clicked:
    # 4) dump 弹窗结构
    print("\n>>> booking dialog structure")
    dlg = page.evaluate("""() => {
        const wrap = document.querySelector('.el-dialog, .el-message-box, [class*=dialog], [class*=modal]');
        const res = {found: !!wrap};
        if (wrap) {
            res.text = wrap.innerText.slice(0, 400);
            // 找确认/预约按钮
            const btns = Array.from(wrap.querySelectorAll('button'));
            res.buttons = btns.map(b => ({text: b.innerText.trim(), cls: (b.className||'').toString().slice(0,40)}));
            // 找时间选择
            res.selects = Array.from(wrap.querySelectorAll('.el-select, input')).map(s => ({tag:s.tagName, ph:s.placeholder||'', cls:(s.className||'').toString().slice(0,30)}));
        }
        return res;
    }""")
    print("  dialog:", json.dumps(dlg, ensure_ascii=False, indent=2))
    (DBG / "flow_dialog.json").write_text(json.dumps(dlg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5) 点击确认/预约按钮 -> 触发 TAC 滑块
    print("\n>>> click confirm to trigger slider (NO drag)")
    try:
        loc = page.get_by_text("确认", exact=True).first
        if loc.count() and loc.is_visible():
            loc.click(timeout=4000)
        else:
            # 尝试其他文案
            for txt in ["预约", "提交", "确定", "立即预约"]:
                l2 = page.get_by_text(txt, exact=True).first
                if l2.count() and l2.is_visible():
                    l2.click(timeout=4000)
                    break
        page.wait_for_timeout(3000)
        shot("flow_03_slider")
    except Exception as exc:
        print("  confirm click err:", exc)
    # 6) 探查 TAC 滑块 DOM
    print("\n>>> TAC slider DOM")
    tac = page.evaluate("""() => {
        const wrap = document.querySelector('#show-code-check-wrap');
        const res = {wrapFound: !!wrap};
        if (wrap) {
            res.html = wrap.outerHTML.slice(0, 1500);
            res.imgs = Array.from(wrap.querySelectorAll('img')).map(i => ({src: (i.src||'').slice(0,60), cls:(i.className||'').toString().slice(0,40), w:i.naturalWidth, h:i.naturalHeight}));
            res.divs = Array.from(wrap.querySelectorAll('div')).slice(0,20).map(d => ({cls:(d.className||'').toString().slice(0,50), w:Math.round(d.getBoundingClientRect().width), h:Math.round(d.getBoundingClientRect().height)}));
        }
        return res;
    }""")
    print("  TAC:", json.dumps(tac, ensure_ascii=False, indent=2)[:1500])
    (DBG / "flow_tac.json").write_text(json.dumps(tac, ensure_ascii=False, indent=2), encoding="utf-8")

sess.close()
print("\n>>> probe finished (no booking submitted)")
