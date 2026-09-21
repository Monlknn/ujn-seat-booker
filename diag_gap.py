"""诊断：对比多种缺口检测算法，并绘制拼图轮廓叠加以肉眼验证。

先运行 probe_slider.py 取得 debug/tac_live_bg.png / tac_live_piece.png，
再运行本脚本。会生成 debug/diag_*.png（把拼图 alpha 轮廓画到背景对应缺口处）。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

DBG = Path("debug")
bg = cv2.imdecode(np.frombuffer((DBG / "tac_live_bg.png").read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
piece = cv2.imdecode(np.frombuffer((DBG / "tac_live_piece.png").read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
if bg.ndim == 3 and bg.shape[2] == 4:
    bg = cv2.cvtColor(bg, cv2.COLOR_BGRA2RGBA)
else:
    bg = cv2.cvtColor(bg, cv2.COLOR_BGR2RGBA)
if piece.ndim == 3 and piece.shape[2] == 4:
    piece = cv2.cvtColor(piece, cv2.COLOR_BGRA2RGBA)
else:
    piece = cv2.cvtColor(piece, cv2.COLOR_BGR2RGBA)

bh, bw = bg.shape[:2]
ph, pw = piece.shape[:2]
print(f"bg {bw}x{bh}  piece {pw}x{ph}")

bg_rgb = bg[:, :, :3].astype(np.float32)
bg_gray = cv2.cvtColor(bg, cv2.COLOR_RGBA2GRAY).astype(np.float32)
piece_gray = cv2.cvtColor(piece, cv2.COLOR_RGBA2GRAY).astype(np.float32)
piece_rgb = piece[:, :, :3].astype(np.float32)
alpha = (piece[:, :, 3] > 30).astype(np.uint8)
mask = alpha.astype(bool)
piece_mean = piece_rgb[mask].mean(axis=0)

# 梯度幅值（用于边界检测）
bg_sobel_x = cv2.Sobel(bg_gray, cv2.CV_32F, 1, 0, ksize=3)
bg_sobel_y = cv2.Sobel(bg_gray, cv2.CV_32F, 0, 1, ksize=3)
bg_grad = cv2.magnitude(bg_sobel_x, bg_sobel_y)

# alpha 的边界环
def ring(k_inner=1, k_outer=3):
    inner = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=k_inner)
    outer = cv2.dilate(alpha, np.ones((3, 3), np.uint8), iterations=k_outer)
    return ((outer - inner) > 0).astype(np.uint8)

ring_u8 = ring(1, 3)
ring_pixels = int(ring_u8.sum())
print(f"ring pixels={ring_pixels}")


def ncc_intensity():
    res = cv2.matchTemplate(bg_gray.astype(np.uint8), piece_gray.astype(np.uint8),
                           cv2.TM_CCOEFF_NORMED, mask=alpha)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return int(max_loc[0]), float(max_val)


def edge_grad_ring():
    """用拼图 alpha 外环与背景梯度幅值做 NCC。"""
    res = cv2.matchTemplate(bg_grad.astype(np.uint8), ring_u8 * 255,
                           cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return int(max_loc[0]), float(max_val)


def boundary_uniformity():
    """检测"边界强、内部均匀"的 region：score = boundary_strength / (1+interior_std)。"""
    # 内部 kernel
    inner = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=2).astype(bool)
    outer = cv2.dilate(alpha, np.ones((3, 3), np.uint8), iterations=2).astype(bool)
    ring_mask = (outer & ~mask)  # 外环（紧邻内部）
    scores = []
    for x in range(bw - pw + 1):
        patch_grad = bg_grad[:, x:x + pw]
        patch_rgb = bg_rgb[:, x:x + pw, :]
        b_strength = patch_grad[ring_mask].mean() if ring_mask.any() else 0
        in_std = patch_rgb[inner].std(axis=0).mean()
        s = b_strength / (1.0 + in_std)
        scores.append(s)
    scores = np.array(scores)
    return int(np.argmax(scores)), float(scores.max()), scores


def darkened_ncc():
    """把拼图按多种系数变暗后分别做 NCC，取最佳。"""
    best = (0, 0.0, 0.0)
    for factor in [0.2, 0.35, 0.5, 0.65, 0.8, 1.0]:
        dark = (piece_gray * factor).astype(np.uint8)
        res = cv2.matchTemplate(bg_gray.astype(np.uint8), dark, cv2.TM_CCOEFF_NORMED, mask=alpha)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best[1]:
            best = (int(max_loc[0]), float(max_val), factor)
    return best


def boundary_direction():
    """沿 alpha 边界，比较边界两侧（内 vs 外）的平均亮度差异与边界梯度。
    真正的缺口在边界处有强烈跳变，内部相对均匀。
    """
    inner = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=1).astype(bool)
    outer = cv2.dilate(alpha, np.ones((5, 5), np.uint8), iterations=1).astype(bool)
    ring_outer = (outer & ~mask)
    boundary = ring_u8.astype(bool)
    scores = []
    for x in range(bw - pw + 1):
        patch_gray = bg_gray[:, x:x + pw]
        patch_rgb = bg_rgb[:, x:x + pw, :]
        in_mean = patch_gray[inner].mean()
        out_mean = patch_gray[ring_outer].mean()
        grad_mean = bg_grad[:, x:x + pw][boundary].mean()
        in_std = patch_gray[inner].std()
        contrast = abs(out_mean - in_mean)
        # 边界梯度强 + 内外对比大 + 内部均匀 -> 缺口
        s = grad_mean * contrast / (1.0 + in_std)
        scores.append(s)
    scores = np.array(scores)
    return int(np.argmax(scores)), float(scores.max()), scores


def boundary_combined():
    """综合：边界梯度 + 内外对比 + 内部与拼图内容差异 + 内部均匀。"""
    inner = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=1).astype(bool)
    outer = cv2.dilate(alpha, np.ones((5, 5), np.uint8), iterations=1).astype(bool)
    ring_outer = (outer & ~mask)
    boundary = ring_u8.astype(bool)
    scores = []
    for x in range(bw - pw + 1):
        patch_gray = bg_gray[:, x:x + pw]
        patch_rgb = bg_rgb[:, x:x + pw, :]
        in_mean_g = patch_gray[inner].mean()
        in_std_g = patch_gray[inner].std()
        out_mean_g = patch_gray[ring_outer].mean()
        in_mean_rgb = patch_rgb[inner].mean(axis=0)
        grad_mean = bg_grad[:, x:x + pw][boundary].mean()
        contrast = abs(out_mean_g - in_mean_g)
        diff_piece = float(np.linalg.norm(in_mean_rgb - piece_mean))
        s = grad_mean * contrast * diff_piece / (1.0 + in_std_g)
        scores.append(s)
    scores = np.array(scores)
    return int(np.argmax(scores)), float(scores.max()), scores


def draw_overlay(gap_x, name):
    ys, xs_mask = np.where(mask)
    if len(xs_mask) == 0:
        return
    minx, maxx = xs_mask.min(), xs_mask.max()
    miny, maxy = ys.min(), ys.max()
    crop_alpha = alpha[miny:maxy + 1, minx:maxx + 1]
    cnts, _ = cv2.findContours((crop_alpha > 30).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    overlay = bg[:, :, :3].copy()
    rgb = piece[:, :, :3]
    a = piece[:, :, 3:4].astype(np.float32) / 255.0
    y0, y1 = miny, maxy + 1
    x0, x1 = gap_x + minx, gap_x + minx + (maxx - minx + 1)
    if y1 > overlay.shape[0]:
        y1 = overlay.shape[0]
        y0 = y1 - (maxy - miny + 1)
    if x1 > overlay.shape[1]:
        x1 = overlay.shape[1]
    reg = overlay[y0:y1, x0:x1]
    ah = min(reg.shape[0], rgb.shape[0] - miny)
    aw = min(reg.shape[1], rgb.shape[1] - minx)
    if ah > 0 and aw > 0:
        r = rgb[miny:miny + ah, minx:minx + aw]
        al = a[miny:miny + ah, minx:minx + aw]
        reg[:ah, :aw] = (reg[:ah, :aw].astype(np.float32) * (1 - al[:, :aw]) + r.astype(np.float32) * al[:, :aw]).astype(np.uint8)
    for c in cnts:
        c2 = c.copy()
        c2[:, :, 0] += (gap_x + minx)
        c2[:, :, 1] += miny
        cv2.polylines(overlay, [c2], True, (255, 0, 0), 2)
    out = cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR)
    (DBG / name).write_bytes(cv2.imencode(".png", out)[1])


# 计算各方法
gx_ncc, sv_ncc = ncc_intensity()
gx_edge, sv_edge = edge_grad_ring()
gx_bu, sv_bu, arr_bu = boundary_uniformity()
gx_dark, sv_dark, factor = darkened_ncc()
gx_dir, sv_dir, arr_dir = boundary_direction()
gx_comb, sv_comb, arr_comb = boundary_combined()

print(f"NCC(intensity):    gap_x={gx_ncc:3d}  score={sv_ncc:.3f}")
print(f"Edge(grad ring):   gap_x={gx_edge:3d}  score={sv_edge:.3f}")
print(f"BoundaryUniform:   gap_x={gx_bu:3d}  score={sv_bu:.1f}")
print(f"Darkened(f={factor}):   gap_x={gx_dark:3d}  score={sv_dark:.3f}")
print(f"BoundaryDir:       gap_x={gx_dir:3d}  score={sv_dir:.1f}")
print(f"BoundaryCombined:  gap_x={gx_comb:3d}  score={sv_comb:.1f}")

draw_overlay(gx_ncc, "diag_ncc.png")
draw_overlay(gx_edge, "diag_edge.png")
draw_overlay(gx_bu, "diag_boundary_uniform.png")
draw_overlay(gx_dark, "diag_darkened.png")
draw_overlay(gx_dir, "diag_boundary_dir.png")
draw_overlay(gx_comb, "diag_boundary_combined.png")
print("saved all overlays")
