"""Offline validation of the slider gap-offset computation.

Uses the saved gen/SLIDER response (debug/api_genSlider.json) which contains a real
captcha: backgroundImage (600x360 jpeg) + templateImage (110x360 png), data empty.
We template-match the piece against the background to find the gap x-offset, and
visualize the matched region so we can sanity-check.
"""
import base64
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

DBG = Path(__file__).resolve().parent / "debug"
g = json.loads((DBG / "api_genSlider.json").read_text(encoding="utf-8"))
cap = g["captcha"]
print("captcha keys:", list(cap.keys()))
print("type:", cap.get("type"), "bg:", cap.get("backgroundImageWidth"), "x", cap.get("backgroundImageHeight"),
      "piece:", cap.get("templateImageWidth"), "x", cap.get("templateImageHeight"))
print("data field empty?:", cap.get("data") == "")


def _b64_to_cv(b64: str):
    # strip data: prefix
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    raw = base64.b64decode(b64)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)  # keep alpha if png


bg = _b64_to_cv(cap["backgroundImage"])
piece = _b64_to_cv(cap["templateImage"])
print("bg shape:", bg.shape, "piece shape:", piece.shape, "piece channels:", piece.shape[-1] if piece.ndim == 3 else 1)

# Convert to gray for matching; for the piece, use alpha as mask if present.
bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY) if bg.ndim == 3 else bg
if piece.shape[-1] == 4:
    piece_rgb = cv2.cvtColor(piece[..., :3], cv2.COLOR_BGR2GRAY)
    mask = piece[..., 3]
    # mask the piece's transparent area to neutral gray so matching focuses on the piece shape
    piece_gray = piece_rgb.copy()
else:
    piece_gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY) if piece.ndim == 3 else piece
    mask = None

# Edge enhance
bg_e = cv2.Canny(bg_gray, 100, 200)
piece_e = cv2.Canny(piece_gray, 100, 200)

res = cv2.matchTemplate(bg_e, piece_e, cv2.TM_CCOEFF_NORMED)
_, max_val, _, max_loc = cv2.minMaxLoc(res)
gap_x, gap_y = max_loc
print(f"TM_CCOEFF_NORMED -> gap_x={gap_x} gap_y={gap_y} score={max_val:.3f}")

# Also try TM_SQDIFF for cross-check
res2 = cv2.matchTemplate(bg_e, piece_e, cv2.TM_SQDIFF_NORMED)
min_val, _, min_loc, _ = cv2.minMaxLoc(res2)
print(f"TM_SQDIFF_NORMED -> gap_x={min_loc[0]} gap_y={min_loc[1]} score={min_val:.3f}")

# Visualize: draw rectangle of piece size at detected gap_x on a copy of bg
vis = bg.copy() if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
pw = cap["templateImageWidth"]
ph = cap["templateImageHeight"]
cv2.rectangle(vis, (gap_x, gap_y), (gap_x + pw, gap_y + ph), (0, 0, 255), 2)
out = DBG / "match_debug.png"
cv2.imwrite(str(out), vis)
print("saved visualization:", out)

# Report the fraction of width where the gap sits (to map to displayed slider width)
print(f"gap_x fraction of bg width ({cap['backgroundImageWidth']}): {gap_x/cap['backgroundImageWidth']:.3f}")
