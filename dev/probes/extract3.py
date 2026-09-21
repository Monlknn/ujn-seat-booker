"""Inspect how fabric seat objects are created: is o.id the DB seat id?"""
from pathlib import Path

src = (Path(__file__).resolve().parents[2] / "debug" / "app.js.txt").read_text(encoding="utf-8", errors="replace")

def window(anchor, before=300, after=1100, tag=""):
    i = src.find(anchor)
    print(f"\n===== [{tag}] {anchor!r} @ {i} =====")
    if i < 0:
        print("(not found)")
        return
    s = max(0, i - before)
    e = min(len(src), i + after)
    print(src[s:e])

# getLayOutData + seat creation loop
window("getLayOutData:function", before=200, after=2400, tag="getLayOutData")
# new fabric.LabeledImage with options (look for id assignment)
window("new fabric.LabeledImage", before=200, after=600, tag="newLabeledImage")
window("LabeledImage(", before=200, after=500, tag="LabeledImage(")
# how seats are added
window("seat.id", before=200, after=400, tag="seat.id")
window(".seat.id", before=200, after=300, tag=".seat.id")
# initSeat / SeatPreview
window("initSeat", before=100, after=900, tag="initSeat")
window("SeatPreview", before=100, after=700, tag="SeatPreview")
