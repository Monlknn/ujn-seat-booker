"""浏览器探查 v3：捕获网络请求、REST layout API、canvas 结构、座位点击弹窗/滑块。"""
import sys
import json
import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config
from src.session import login_browser

ROOT = Path(__file__).resolve().parents[2]
DBG = ROOT / "debug"
DBG.mkdir(exist_ok=True)

cfg = Config.load(ROOT / "config.json")
sess = login_browser(cfg, headless=False)
page = sess.page
context = sess.context

BASE = "https://seat.ujn.edu.cn"
network_log = []


def on_response(response):
    url = response.url
    if "seat.ujn.edu.cn" not in url:
        return
    try:
        body = response.body()
        text = body.decode("utf-8", errors="ignore")
        network_log.append({
            "url": url,
            "status": response.status,
            "text": text[:6000] if len(text) < 200_000 else text[:200],
        })
    except Exception as exc:  # noqa: BLE001
        network_log.append({"url": url, "status": response.status, "text": f"<err:{exc}>"})


page.on("response", on_response)


def shot(name: str):
    try:
        page.screenshot(path=str(DBG / f"{name}.png"), full_page=False)
        print(f"[shot] {name}.png")
    except Exception as exc:  # noqa: BLE001
        print("  shot err:", exc)


def close_drawer():
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:  # noqa: BLE001
        pass


def cookies_dict():
    return {c["name"]: c["value"] for c in context.cookies()}


def api_get(path: str):
    url = BASE + path
    try:
        r = requests.get(url, cookies=cookies_dict(), timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"  API {path} -> ERROR {exc}")
        return None
    print(f"  API {path} -> {r.status_code}, len={len(r.text)}")
    return r


# 1) 主界面
print(">>> open jsq-v main")
page.goto("https://seat.ujn.edu.cn/jsq-v/#/", wait_until="domcontentloaded", timeout=20000)
page.wait_for_timeout(3000)
close_drawer()
shot("v3_01_main")
print("Main URL:", page.url)

# 2) 点击第一阅览室卡片
print("\n>>> click 第一阅览室")
try:
    page.get_by_text("第一阅览室", exact=True).first.click(timeout=5000)
    page.wait_for_timeout(3500)
except Exception as exc:  # noqa: BLE001
    print("  click room err:", exc)
print("Room URL:", page.url)
shot("v3_02_room")

with open(DBG / "v3_network.json", "w", encoding="utf-8") as f:
    json.dump(network_log, f, ensure_ascii=False, indent=2)
print(f"[net] saved {len(network_log)} responses")

# 3) REST API 探查（用浏览器 cookie）
print("\n>>> REST API introspection")
rooms_resp = api_get("/rest/v2/rooms")
room_id = None
if rooms_resp and rooms_resp.status_code == 200:
    (DBG / "v3_rooms.json").write_text(rooms_resp.text, encoding="utf-8")
    try:
        rooms_data = rooms_resp.json()
        print("  rooms keys:", list(rooms_data.keys())[:10])
        rooms = rooms_data.get("data") or []
        print("  rooms count:", len(rooms))
        for r in rooms[:8]:
            print("    room:", {k: r.get(k) for k in ["id", "name", "floor", "room"] if k in r})
        target = cfg.get("room", "第一阅览室")
        for r in rooms:
            if target in (r.get("name") or "") or target in (r.get("room") or ""):
                room_id = r.get("id")
                break
    except Exception as exc:  # noqa: BLE001
        print("  parse rooms err:", exc)
print(f"  room_id for target room = {room_id}")

if room_id:
    date_str = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    layout_resp = api_get(f"/rest/v2/room/layoutByDate/{room_id}/{date_str}")
    if layout_resp and layout_resp.status_code == 200:
        (DBG / "v3_layout.json").write_text(layout_resp.text, encoding="utf-8")
        try:
            layout = layout_resp.json()
            print("  layout keys:", list(layout.keys())[:10])
            d = layout.get("data")
            if isinstance(d, dict):
                print("  data keys:", list(d.keys())[:20])
                seats = d.get("layout")
                if isinstance(seats, dict):
                    print("  seat count:", len(seats))
                    for k, v in list(seats.items())[:3]:
                        print(f"    seat {k} sample:", v)
        except Exception as exc:  # noqa: BLE001
            print("  parse layout err:", exc)

# 4) canvas 探查
print("\n>>> Canvas introspection")
canvas_info = page.evaluate("""() => {
    const res = {found:false};
    const container = document.querySelector('.seatlayoutcanvascontainer');
    res.container_found = !!container;
    if (container) {
        const r = container.getBoundingClientRect();
        res.container_rect = {x:r.x, y:r.y, w:r.width, h:r.height};
        res.container_html = container.outerHTML.slice(0, 400);
        const cvs = container.querySelector('canvas');
        res.canvas_found = !!cvs;
        if (cvs) {
            res.canvas_size = {w:cvs.width, h:cvs.height};
            const cr = cvs.getBoundingClientRect();
            res.canvas_rect = {x:cr.x, y:cr.y, w:cr.width, h:cr.height};
        }
        function walk(el, depth) {
            if (depth>8 || res.vue_keys) return;
            if (el.__vue__) {
                res.vue_keys = Object.keys(el.__vue__);
                res.vue_data_keys = el.__vue__.$data ? Object.keys(el.__vue__.$data) : [];
                return;
            }
            for (const c of el.children) walk(c, depth+1);
        }
        walk(container, 0);
    }
    return res;
}""")
print("  canvas_info:", json.dumps(canvas_info, ensure_ascii=False, indent=2))

# 5) 点击 canvas 中心，看是否弹出预约弹窗（绝不点提交）
print("\n>>> Click canvas center (no submit)")
try:
    cvs = page.locator(".seatlayoutcanvascontainer canvas").first
    if cvs.count():
        box = cvs.bounding_box()
        print("  canvas bbox:", box)
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.click(cx, cy)
        page.wait_for_timeout(3000)
        shot("v3_03_after_click")
        for dsel in [".el-dialog", ".el-message-box", ".dialog", ".modal", "[class*='dialog']", "[class*='modal']"]:
            dl = page.locator(dsel).first
            if dl.count() and dl.is_visible():
                print(f"  Dialog ({dsel}):", dl.inner_text()[:600])
                break
        slider = page.evaluate("""() => {
            const sels = ['.slider-btn','.slide-block','#slider','.nc_iconfont.btn_slide','.yidun_slider','.captcha-slider-btn','.slider-block','.verify-slider','.slider-container','.slider','.drag-btn','.handler','.slide-verify','.secs-slider','.slider_verify','.move-btn','.btn_slide'];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el) { const r = el.getBoundingClientRect(); return {sel:s, x:r.x, y:r.y, w:r.width, h:r.height}; }
            }
            return null;
        }""")
        print("  slider found:", slider)
except Exception as exc:  # noqa: BLE001
    print("  click canvas err:", exc)

(DBG / "v3_final.html").write_text(page.content(), encoding="utf-8")
print("\n[html] saved v3_final.html")
print(">>> Explorer finished (no booking submitted)")

sess.close()
