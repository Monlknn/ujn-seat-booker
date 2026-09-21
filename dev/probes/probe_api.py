"""用 sessionStorage 的 token 调用 gen/SLIDER 与 querySeatLayout，分析滑块答案与座位坐标。"""
import sys
import json
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config
from src.session import login_browser

ROOT = Path(__file__).resolve().parents[2]
DBG = ROOT / "debug"
DBG.mkdir(exist_ok=True)

cfg = Config.load(ROOT / "config.json")
sess = login_browser(cfg, headless=False)
page = sess.page

date_str = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
room_id = "2067507551033479168"


def get_token():
    return page.evaluate("() => sessionStorage.getItem('token')")


def api(path, method="GET", body=None, label="x", token=None):
    url = "https://seat.ujn.edu.cn" + path
    js_body = {"method": method, "credentials": "include",
               "headers": {"Accept": "application/json", "Content-Type": "application/json",
                           "token": token or ""}}
    if body is not None:
        js_body["body"] = json.dumps(body)
    try:
        text = page.evaluate("""async (args) => {
            const r = await fetch(args.url, args.init);
            return await r.text();
        }""", {"url": url, "init": js_body})
    except Exception as exc:
        print(f"--- {label}: FETCH ERROR {exc}")
        return None
    print(f"--- {label}: {method} {path} -> len={len(text)}")
    p = DBG / f"api_{label}.json"
    p.write_text(text, encoding="utf-8")
    try:
        j = json.loads(text)
        if isinstance(j, dict):
            print("    top keys:", list(j.keys())[:15])
            d = j.get("data")
            if isinstance(d, dict):
                # 不打印长 base64
                small = {k: (str(v)[:80] if not isinstance(v, (dict, list)) else f"<{type(v).__name__} len={len(v)}>") for k, v in d.items()}
                print("    data keys:", small)
            elif isinstance(d, str):
                print("    data(str) len:", len(d), "head:", d[:80])
            elif isinstance(d, list):
                print("    data list len:", len(d))
        elif isinstance(j, list):
            print("    list len:", len(j))
    except Exception:
        print("    not json, head:", text[:200])
    return text


print(">>> navigate into SPA")
page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
page.wait_for_timeout(4000)
token = get_token()
print("token:", (token or "")[:40], "...")

# 滑块数据
api("/jsq/static/cap/cg/gen/SLIDER", "POST", {"username": cfg.username}, "genSlider", token)
# 座位布局
api(f"/jsq/static/frontApi/res/querySeatLayout/{room_id}/0", "POST", {}, "querySeatLayout", token)
# 空闲座位
api(f"/jsq/static/frontApi/res/freeSeatIdsDuration/{room_id}/{date_str}", "POST", {}, "freeSeatIds", token)

sess.close()
print(">>> done")
