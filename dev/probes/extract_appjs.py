"""Extract context windows around critical keywords from the minified SPA bundle."""
import json
from pathlib import Path

TXT = Path(__file__).resolve().parents[2] / "debug" / "app.js.txt"
src = TXT.read_text(encoding="utf-8", errors="replace")
print("bundle length:", len(src))

# Find the region around #seat.ujn.edu.cn references to learn API base + cap endpoints
keywords = [
    "xReserveSeat", "confirmFilter", "codeCheck", "freeBook", "capToken",
    "mackCaptcha", "systemInfo", "validSuccess", "validCaptchaUrl",
    "requestCaptchaDataUrl", "show-code-check-wrap", "touchSelected",
    "querySeatLayout", "gen/SLIDER", "check/SLIDER", "/static/cap",
    "LabeledImage", "seatPreview", "reserveSeat", "makeDate",
    "startMinute", "endMinute", "selectSeat", "openSeat",
]
COLS = 220
for kw in keywords:
    idx = 0
    count = 0
    while True:
        i = src.find(kw, idx)
        if i < 0:
            break
        count += 1
        if count <= 2:  # up to 2 occurrences per keyword
            s = max(0, i - COLS)
            e = min(len(src), i + COLS)
            snippet = src[s:e]
            # trim to nearest whitespace-ish for readability
            print(f"\n===== [{kw}] #{count} @ {i} =====")
            print(snippet)
        idx = i + len(kw)
    if count == 0:
        print(f"\n----- [{kw}] NOT FOUND -----")

# Inspect saved gen/SLIDER response (captcha) keys
print("\n\n##### api_genSlider.json keys #####")
try:
    g = json.loads((Path(__file__).resolve().parents[2] / "debug" / "api_genSlider.json").read_text(encoding="utf-8"))
    def walk(o, prefix=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    print(f"{prefix}{k}: {type(v).__name__}(len={len(v)})")
                    if isinstance(v, dict):
                        walk(v, prefix + "  ")
                else:
                    sv = str(v)
                    print(f"{prefix}{k}: {sv[:80]}")
        elif isinstance(o, list) and o:
            print(f"{prefix}[list len={len(o)}] first item type={type(o[0]).__name__}")
            if isinstance(o[0], dict):
                walk(o[0], prefix + "  ")
    walk(g)
except Exception as exc:
    print("err:", exc)
