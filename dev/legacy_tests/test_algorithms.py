"""比较不同缺口检测算法在真实 TAC 图片上的表现。

保存了 debug/tac_live_bg.png + tac_live_piece.png 后运行。
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
from PIL import Image
from src.slider import _load_image

DBG = Path(__file__).resolve().parents[2] / "debug"
bg_b = (DBG / "tac_live_bg.png").read_bytes()
piece_b = (DBG / "tac_live_piece.png").read_bytes()

bg = _load_image(bg_b)
piece = _load_image(piece_b)
bg_gray = bg[..., :3].mean(axis=2).astype(np.float32)
piece_gray = piece[..., :3].mean(axis=2).astype(np.float32)

bh, bw = bg_gray.shape
ph, pw = piece_gray.shape

import cv2
mask_u8 = ((piece[..., 3] > 128).astype(np.uint8)) * 255
# 内环（腐蚀）与外环（膨胀减原）
inner_u8 = cv2.erode(mask_u8, np.ones((5, 5), np.uint8), iterations=2)
outer_u8 = cv2.dilate(mask_u8, np.ones((5, 5), np.uint8), iterations=2) - mask_u8
inner = inner_u8.astype(bool)
outer = outer_u8.astype(bool)
mask = (piece[..., 3] > 30).astype(bool)
print("mask non-transparent pixels:", mask.sum(), "inner:", inner.sum(), "outer ring:", outer.sum())


def score_ncc():
    """当前 alpha-masked 归一化互相关（问题算法）。"""
    import cv2
    m = mask.astype(np.uint8)
    res = cv2.matchTemplate(bg_gray.astype(np.uint8), piece_gray.astype(np.uint8),
                            cv2.TM_CCOEFF_NORMED, mask=m)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return max_loc[0], max_val


def score_boundary_edge():
    """缺口边界边缘匹配：用 Canny 检测背景边缘，与拼图边界做相关。"""
    import cv2
    # 拼图边界 = 膨胀 - 腐蚀
    bnd_u8 = (cv2.dilate(mask_u8, np.ones((5, 5), np.uint8), iterations=2) -
              cv2.erode(mask_u8, np.ones((5, 5), np.uint8), iterations=1))
    boundary = bnd_u8.astype(np.uint8)
    # 背景 Canny 边缘
    bg_edges = cv2.Canny(bg_gray.astype(np.uint8), 50, 150)
    res = cv2.matchTemplate(bg_edges, boundary, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return max_loc[0], max_val


def score_texture_contrast():
    """内-外纹理对比：缺口内部均匀，外部有纹理。"""
    best_x, best_score = -1, -float('inf')
    scores = []
    for x in range(bw - pw + 1):
        patch = bg_gray[:, x:x + pw]
        in_std = patch[inner].std()
        out_std = patch[outer].std()
        # 我们希望内部 std 低，外部 std 高
        score = (out_std - in_std) / max(out_std + in_std, 1e-9)
        scores.append(score)
        if score > best_score:
            best_score = score
            best_x = x
    return best_x, best_score, scores


def score_mean_diff():
    """缺口内部均值与外部均值差异最大化。"""
    best_x, best_score = -1, -float('inf')
    scores = []
    for x in range(bw - pw + 1):
        patch = bg_gray[:, x:x + pw]
        in_mean = patch[inner].mean()
        out_mean = patch[outer].mean()
        score = abs(in_mean - out_mean)
        scores.append(score)
        if score > best_score:
            best_score = score
            best_x = x
    return best_x, best_score, scores


def score_combined():
    """综合边界边缘 + 内-外纹理对比 + 均值差异。"""
    bx, _, _ = score_boundary_edge()
    tx, _, ts = score_texture_contrast()
    mx, _, ms = score_mean_diff()
    # 归一化
    ts = np.array(ts)
    ms = np.array(ms)
    ts_n = (ts - ts.min()) / (ts.max() - ts.min() + 1e-9)
    ms_n = (ms - ms.min()) / (ms.max() - ms.min() + 1e-9)
    # 边界边缘无法取到逐 x 分数，单独返回
    return bx, tx, mx, ts_n, ms_n


def save_overlay(x, name):
    base = Image.open(DBG / "tac_live_bg.png").convert("RGBA")
    p = Image.open(DBG / "tac_live_piece.png").convert("RGBA")
    o = base.copy()
    o.paste(p, (x, 0), p)
    o.save(DBG / name)


print("NCC:", score_ncc())
print("Boundary edge:", score_boundary_edge())
tx, ts, _ = score_texture_contrast()
print("Texture contrast:", tx, "score:", ts)
mx, ms, _ = score_mean_diff()
print("Mean diff:", mx, "score:", ms)

bx = score_boundary_edge()[0]
tx2, _, _ = score_texture_contrast()
mx2, _, _ = score_mean_diff()
_, _, ts = score_texture_contrast()
_, _, ms = score_mean_diff()
ts_n = (np.array(ts) - np.min(ts)) / (np.max(ts) - np.min(ts) + 1e-9)
ms_n = (np.array(ms) - np.min(ms)) / (np.max(ms) - np.min(ms) + 1e-9)
combined = ts_n + ms_n
best_combined = int(np.argmax(combined))
print("Boundary:", bx, "Texture:", tx2, "Mean:", mx2, "Combined:", best_combined)

save_overlay(bx, "tac_live_overlay_boundary.png")
save_overlay(tx2, "tac_live_overlay_texture.png")
save_overlay(mx2, "tac_live_overlay_meandiff.png")
save_overlay(best_combined, "tac_live_overlay_combined.png")
print("saved overlays")
