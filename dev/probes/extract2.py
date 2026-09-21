"""Extract the reserve dialog `open` method + openCaptcha + start-time fetch to confirm seat.id source."""
from pathlib import Path

src = (Path(__file__).resolve().parents[2] / "debug" / "app.js.txt").read_text(encoding="utf-8", errors="replace")

def window(anchor, before=600, after=900):
    i = src.find(anchor)
    print(f"\n===== anchor: {anchor!r} @ {i} =====")
    if i < 0:
        print("(not found)")
        return
    s = max(0, i - before)
    e = min(len(src), i + after)
    print(src[s:e])

# reserve dialog open() and seat assignment
window("open:function(t){this.show=!0", before=200, after=900)
# alternative open signatures
window("open:function(t){", before=120, after=700)
# this.seat= assignment in reserve dialog
window("this.seat=t", before=200, after=400)
window("this.seat=t.seat", before=200, after=400)
window("this.seat=a", before=200, after=400)
# openCaptcha definition
window("openCaptcha:function", before=80, after=700)
# getStartTimes usage in reserve
window("getStartTimes", before=80, after=500)
# starTimeValue / endTimeValue set
window("starTimeValue", before=120, after=400)
# sessionStorageProxy definition
window("sessionStorageProxy", before=40, after=400)
