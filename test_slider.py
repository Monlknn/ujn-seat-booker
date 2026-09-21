"""Quick sanity check of src.slider._match_offset with the saved real captcha."""
import base64
import json
from pathlib import Path

from src.slider import _match_offset

DBG = Path(__file__).resolve().parent / "debug"
g = json.loads((DBG / "api_genSlider.json").read_text(encoding="utf-8"))
cap = g["captcha"]

def dec(k):
    src = cap[k]
    return base64.b64decode(src.split(",", 1)[1])

bg_b = dec("backgroundImage")
piece_b = dec("templateImage")
print("bg bytes:", len(bg_b), "piece bytes:", len(piece_b))

offset = _match_offset(bg_b, piece_b)
print("matched gap_x:", offset, "fraction:", offset / cap["backgroundImageWidth"] if offset else None)
