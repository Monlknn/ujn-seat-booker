"""Improved slider gap-offset test using alpha-masked normalized cross-correlation."""
import base64
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

DBG = Path(__file__).resolve().parent / "debug"
g = json.loads((DBG / "api_genSlider.json").read_text(encoding="utf-8"))
cap = g["captcha"]


def decode(src: str):
    if "," in src:
        src = src.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(src.strip())))


import io

bg = decode(cap["backgroundImage"])
piece = decode(cap["templateImage"])
print("bg:", bg.size, "piece:", piece.size, "piece mode:", piece.mode)

bg_arr = np.array(bg.convert("RGBA"))
piece_arr = np.array(piece)

bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGBA2GRAY)
piece_gray = cv2.cvtColor(piece_arr, cv2.COLOR_RGBA2GRAY)
mask = (piece_arr[:, :, 3] > 50).astype(np.uint8)

# Method 1: OpenCV with mask
res = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED, mask=mask)
_, max_val, _, max_loc = cv2.minMaxLoc(res)
print(f"OpenCV TM_CCOEFF_NORMED with mask -> gap_x={max_loc[0]} score={max_val:.3f}")

# Method 2: numpy masked NCC
from numpy.lib.stride_tricks import sliding_window_view
patches = sliding_window_view(bg_gray, piece_gray.shape)  # (1, 491, 360, 110)
patches = patches.squeeze()  # (491, 360, 110)
mask_bool = piece_arr[:, :, 3] > 50
n = mask_bool.sum()
print("valid piece pixels:", n)
patches_masked = patches[:, mask_bool]  # (491, n)
piece_valid = piece_gray[mask_bool]
mean_p = patches_masked.mean(axis=1, keepdims=True)
std_p = patches_masked.std(axis=1, keepdims=True)
std_p[std_p == 0] = 1e-9
mean_t = piece_valid.mean()
std_t = piece_valid.std()
num = ((patches_masked - mean_p) * (piece_valid - mean_t)).sum(axis=1)
den = std_p.squeeze() * std_t * math.sqrt(n)
score = num / den
best_x = int(np.argmax(score))
print(f"NumPy masked NCC -> gap_x={best_x} score={score[best_x]:.3f}")

# Visualize best
vis = bg_arr.copy()
cv2.rectangle(vis, (best_x, 0), (best_x + piece_gray.shape[1], piece_gray.shape[0]), (0, 0, 255), 2)
cv2.imwrite(str(DBG / "match2_debug.png"), cv2.cvtColor(vis, cv2.COLOR_RGBA2BGR))
print("saved match2_debug.png")
