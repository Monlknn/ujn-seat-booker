"""打印当前实时图片在各 x 位置的综合分数，帮助定位算法为何失败。"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
from src.slider import _load_image

DBG = Path(__file__).resolve().parents[2] / "debug"
bg_b = (DBG / "tac_live_bg.png").read_bytes()
piece_b = (DBG / "tac_live_piece.png").read_bytes()

bg = _load_image(bg_b)
piece = _load_image(piece_b)

# 手动复现 _match_offset_hole 计算
import cv2
bg_rgb = bg[..., :3].astype(np.float32)
piece_rgb = piece[..., :3].astype(np.float32)
ph, pw = piece_rgb.shape[:2]
bh, bw = bg_rgb.shape[:2]

mask_u8 = ((piece[..., 3] > 128).astype(np.uint8)) * 255
inner_u8 = cv2.erode(mask_u8, np.ones((5, 5), np.uint8), iterations=2)
outer_u8 = cv2.dilate(mask_u8, np.ones((5, 5), np.uint8), iterations=2) - mask_u8
inner = inner_u8.astype(bool)
outer = outer_u8.astype(bool)
inner_n = int(inner.sum())
outer_n = int(outer.sum())
piece_mean = piece_rgb[inner].mean(axis=0)

records = []
for x in range(bw - pw + 1):
    patch = bg_rgb[:, x:x + pw, :]
    in_vals = patch[inner]
    out_vals = patch[outer]
    in_mean = in_vals.mean(axis=0)
    out_mean = out_vals.mean(axis=0)
    in_std = in_vals.std(axis=0).mean()
    out_std = out_vals.std(axis=0).mean()
    diff_piece = float(np.linalg.norm(in_mean - piece_mean) / (255.0 * np.sqrt(3)))
    diff_outer = float(np.linalg.norm(in_mean - out_mean) / (255.0 * np.sqrt(3)))
    uniformity = 1.0 / (1.0 + in_std / 30.0)
    records.append({"x": x, "diff_piece": diff_piece, "diff_outer": diff_outer,
                    "uniformity": uniformity, "in_mean": list(in_mean), "out_mean": list(out_mean),
                    "in_std": in_std, "out_std": out_std})

# 归一化并求 combined
xs = np.array([r["x"] for r in records])
s1 = np.array([r["diff_piece"] for r in records])
s2 = np.array([r["diff_outer"] for r in records])
s3 = np.array([r["uniformity"] for r in records])
def norm(a):
    lo, hi = a.min(), a.max()
    return (a - lo) / max(hi - lo, 1e-9)
s1n = norm(s1)
s2n = norm(s2)
s3n = norm(s3)
combined = s1n + s2n + s3n

for i, r in enumerate(records):
    r["score"] = float(combined[i])

# 排序取前 15
top = sorted(records, key=lambda r: r["score"], reverse=True)[:15]
print("Top 15 x by combined score:")
for r in top:
    print(f"  x={r['x']:3d} score={r['score']:.3f} diff_piece={r['diff_piece']:.3f} diff_outer={r['diff_outer']:.3f} uni={r['uniformity']:.3f} in_mean=({r['in_mean'][0]:6.1f},{r['in_mean'][1]:6.1f},{r['in_mean'][2]:6.1f}) out_mean=({r['out_mean'][0]:6.1f},{r['out_mean'][1]:6.1f},{r['out_mean'][2]:6.1f})")

# 保存完整曲线
(DBG / "tac_live_scores.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
print("saved scores to debug/tac_live_scores.json")
