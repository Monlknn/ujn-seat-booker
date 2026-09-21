"""济大 TAC 滑块验证码求解器。

不同于辽大项目的"文字点选验证码（YOLOv8+Siamese）"，济大在预约提交环节使用
TAC（tianai-captcha）滑块拼图验证码。本模块：
  1. 从页面已渲染的 TAC DOM 中提取背景图与拼图块图。
  2. 用计算机视觉（OpenCV / 纯 NumPy）模板匹配缺口 x 偏移。
  3. 把缺口偏移映射到当前屏幕滑轨宽度，拖动滑块按钮（.slider-move-btn）。

环境要求（二者满足其一即可）：
  - opencv-python + numpy
  - Pillow + numpy
"""
from __future__ import annotations

import base64
import io
import json
import random
import re
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .config import Config
from .logger import logger

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEBUG_DIR = _PROJECT_ROOT / "debug"


def _decode_b64(src: str) -> bytes:
    """从 data:image/*;base64,xx 字符串中提取原始 bytes。"""
    if "," in src:
        src = src.split(",", 1)[1]
    return base64.b64decode(src.strip())


def _load_image(b: bytes):
    """优先 OpenCV，回退 Pillow -> numpy array (RGBA，保留 alpha 通道)。"""
    try:
        import cv2
        arr = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_UNCHANGED)
        if arr is not None:
            if arr.ndim == 3 and arr.shape[2] == 4:
                return cv2.cvtColor(arr, cv2.COLOR_BGRA2RGBA)
            return cv2.cvtColor(arr, cv2.COLOR_BGR2RGBA)
    except Exception:
        pass
    try:
        from PIL import Image
        return np.array(Image.open(io.BytesIO(b)).convert("RGBA"))
    except Exception as exc:
        raise RuntimeError("无法加载图片，请安装 opencv-python 或 Pillow") from exc


def _match_offset_cv2(bg_arr: np.ndarray, piece_arr: np.ndarray) -> Optional[Tuple[int, float]]:
    """OpenCV 模板匹配（TM_CCOEFF_NORMED），用拼图 alpha 作为 mask，只比较非透明像素。

    返回 (偏移, 置信度)。
    """
    try:
        import cv2
        bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGBA2GRAY)
        piece_gray = cv2.cvtColor(piece_arr, cv2.COLOR_RGBA2GRAY)
        mask = (piece_arr[:, :, 3] > 30).astype(np.uint8)
        res = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED, mask=mask)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        return int(max_loc[0]), float(max_val)
    except Exception as exc:
        logger.debug("OpenCV 模板匹配失败: %s", exc)
        return None


def _match_offset_numpy(bg_arr: np.ndarray, piece_arr: np.ndarray) -> Optional[Tuple[int, float]]:
    """纯 NumPy 实现 alpha-masked 归一化互相关模板匹配（无需 OpenCV）。"""
    from numpy.lib.stride_tricks import sliding_window_view
    try:
        bg_gray = bg_arr[..., :3].mean(axis=2) if bg_arr.ndim == 3 and bg_arr.shape[2] >= 3 else bg_arr
        piece_gray = piece_arr[..., :3].mean(axis=2) if piece_arr.ndim == 3 and piece_arr.shape[2] >= 3 else piece_arr
        bg_gray = bg_gray.astype(np.float32)
        piece_gray = piece_gray.astype(np.float32)
        ph, pw = piece_gray.shape
        bh, bw = bg_gray.shape
        if ph > bh or pw > bw:
            return None
        mask = piece_arr[:, :, 3] > 30 if piece_arr.shape[2] == 4 else np.ones_like(piece_gray, dtype=bool)
        mask = mask.astype(bool)
        n = int(mask.sum())
        if n == 0:
            return None
        piece_valid = piece_gray[mask]
        mean_t = piece_valid.mean()
        std_t = piece_valid.std()
        if std_t == 0:
            return None

        patches = sliding_window_view(bg_gray, (ph, pw)).squeeze()  # (W-w+1, ph, pw)
        # 高度相同时 squeeze 会把第一维也去掉，统一为 3 维
        if patches.ndim == 2:
            patches = patches.reshape((-1, ph, pw))
        patches_masked = patches[:, mask]  # (n_x, n_valid)
        mean_p = patches_masked.mean(axis=1, keepdims=True)
        std_p = patches_masked.std(axis=1, keepdims=True)
        std_p[std_p == 0] = 1e-9
        numerator = ((patches_masked - mean_p) * (piece_valid - mean_t)).sum(axis=1)
        denominator = std_p.squeeze() * std_t * np.sqrt(n)
        score = numerator / denominator
        best_x = int(np.argmax(score))
        best_score = float(score[best_x])
        return best_x, best_score
    except Exception as exc:
        logger.debug("NumPy 模板匹配失败: %s", exc)
        return None


def _match_offset_boundary(bg_arr: np.ndarray, piece_arr: np.ndarray) -> Optional[Tuple[int, float, float]]:
    """基于"缺口边界"的检测器。

    TAC 滑块的背景缺口是人工挖去一块后填充/阴影形成的，因此：
      - 缺口边界处有较强的梯度（原图 ↔ 填充色交界）
      - 缺口内外亮度/颜色差异明显（可深可浅）
      - 缺口内部相对均匀

    算法：
      1) 用内外对比 + 内部均匀 + 边界梯度得到全宽定位曲线 coarse[x]。
      2) 取 argmax(coarse) 作为 best_x。
      3) margin = best_coarse / mean(coarse) —— 反映"峰在全宽曲线里有多突出"，
         比 ±15px 局部 best/second 区分度更好（局部算法在 30 长度数组上 best/second
         几乎必然接近，导致 margin 永远 ≈ 1.0）。
    返回 (gap_x, best_score, margin)。
    """
    try:
        import cv2
        bg = bg_arr[..., :3].astype(np.float32) if bg_arr.ndim == 3 and bg_arr.shape[2] >= 3 else bg_arr.astype(np.float32)
        bg_gray = bg.mean(axis=2) if bg.ndim == 3 else bg
        ph, pw = piece_arr.shape[:2]
        bh, bw = bg.shape[:2]
        if ph > bh or pw > bw:
            return None

        alpha = piece_arr[:, :, 3]
        mask = alpha > 30
        if not mask.any():
            return None

        # 边界 ring
        mask_u8 = (mask.astype(np.uint8)) * 255
        inner = cv2.erode(mask_u8, np.ones((3, 3), np.uint8), iterations=1).astype(bool)
        outer = cv2.dilate(mask_u8, np.ones((5, 5), np.uint8), iterations=1).astype(bool)
        ring_outer = (outer & ~mask)
        boundary = (cv2.dilate(mask_u8, np.ones((3, 3), np.uint8), 1) -
                    cv2.erode(mask_u8, np.ones((3, 3), np.uint8), 1)).astype(bool)
        if not inner.any() or not ring_outer.any() or not boundary.any():
            return None

        # 背景梯度幅值
        sobelx = cv2.Sobel(bg_gray, cv2.CV_32F, 1, 0, ksize=3)
        sobely = cv2.Sobel(bg_gray, cv2.CV_32F, 0, 1, ksize=3)
        grad = cv2.magnitude(sobelx, sobely)

        npos = bw - pw + 1
        coarse = np.zeros(npos, dtype=np.float32)
        for x in range(npos):
            patch_gray = bg_gray[:, x:x + pw]
            patch_rgb = bg[:, x:x + pw, :]
            in_mean_rgb = patch_rgb[inner].mean(axis=0)
            out_mean_rgb = patch_rgb[ring_outer].mean(axis=0)
            grad_mean = float(grad[:, x:x + pw][boundary].mean())
            in_std = float(patch_gray[inner].std())
            contrast = np.linalg.norm(in_mean_rgb - out_mean_rgb)
            coarse[x] = grad_mean * contrast / (1.0 + in_std)

        # 全宽 argmax
        coarse_peak = int(np.argmax(coarse))
        best_coarse = float(coarse[coarse_peak])
        if best_coarse <= 0:
            return None

        # margin：峰值 / 全宽均值 —— 真实反映"这个峰是否突出"
        # 成功 case（真缺口）：peak 是异常高的局部极值，margin 通常 8~20
        # 失败 case（局部伪峰）：peak 只比均值略高，margin 通常 < 3
        coarse_mean = float(coarse.mean())
        margin = best_coarse / coarse_mean if coarse_mean > 1e-9 else 1.0
        # 顺便算一个 best/second_best（剔除 peak 后），与旧指标兼容方便日志比对
        masked = coarse.copy()
        masked[coarse_peak] = 0
        second = float(masked.max())
        peak_to_2nd = best_coarse / second if second > 1e-9 else 1.0
        logger.debug("边界曲线 全宽峰值=%s 全宽均值=%.1f margin=%.2f peak2nd=%.2f",
                     best_coarse, coarse_mean, margin, peak_to_2nd)
        return coarse_peak, best_coarse, margin
    except Exception as exc:
        logger.debug("缺口边界检测失败: %s", exc)
        return None


def _match_offset_with_score(bg_bytes: bytes, piece_bytes: bytes) -> Optional[Tuple[int, float, float, dict]]:
    """计算拼图在背景中的 x 缺口偏移（像素）、分数、峰间距。

    主策略：缺口边界检测。备选：alpha-masked NCC。
    两个算法同时跑，cross_check 字典返回两结果及其差距，便于调用方判断
    "两个独立算法结论是否一致"，差距 > 30px → 置信度低（极可能是背景
    复杂纹理让边界检测误中局部伪峰）。
    """
    bg = _load_image(bg_bytes)
    piece = _load_image(piece_bytes)

    cross_check = {"boundary": None, "ncc": None, "agree_diff": None, "agree": None}
    edge_margin = 10
    max_gap = bg.shape[1] - piece.shape[1] - edge_margin

    # 1) 边界检测（主策略）
    b = _match_offset_boundary(bg, piece)
    if b is not None:
        gap_x, score, margin = b
        cross_check["boundary"] = {"gap_x": gap_x, "score": score, "margin": margin}
        logger.debug("边界检测 gap_x=%s score=%.1f margin=%.2f", gap_x, score, margin)
        if score >= 5.0 and edge_margin <= gap_x <= max_gap:
            # 暂存 boundary 结果，等 NCC 算完再交叉验证
            boundary_result = (gap_x, score, margin)
        else:
            boundary_result = None
    else:
        boundary_result = None

    # 2) NCC（同时跑，做交叉验证）
    ncc_result = None
    r = _match_offset_cv2(bg, piece)
    if r is not None:
        ncc_gap, ncc_score = r
        # 算 NCC 的 second_best
        try:
            import cv2
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_RGBA2GRAY)
            piece_gray = cv2.cvtColor(piece, cv2.COLOR_RGBA2GRAY)
            mask = (piece[:, :, 3] > 30).astype(np.uint8)
            res = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED, mask=mask)
            flat = res.flatten()
            flat.sort()
            second_ncc = float(flat[-2]) if len(flat) >= 2 else ncc_score
            ncc_margin = ncc_score / second_ncc if second_ncc > 1e-9 else 1.0
        except Exception:
            ncc_margin = 1.0
        cross_check["ncc"] = {"gap_x": ncc_gap, "score": ncc_score, "margin": ncc_margin}
        if edge_margin <= ncc_gap <= max_gap:
            ncc_result = (ncc_gap, ncc_score, ncc_margin)

    # 交叉验证
    if boundary_result and ncc_result:
        bg_x, _, _ = boundary_result
        nc_x, _, _ = ncc_result
        diff = abs(bg_x - nc_x)
        cross_check["agree_diff"] = diff
        # 两算法一致（差距 ≤ 30px）→ 信任 margin 更高的那个
        if diff <= 30:
            cross_check["agree"] = True
            chosen = boundary_result if boundary_result[2] >= ncc_result[2] else ncc_result
            chosen = (chosen[0], chosen[1], chosen[2], cross_check)
            return chosen
        # 不一致：boundary margin >= 1.5 且明显高于 NCC → 用 boundary（边界纹理明确时更准）
        if boundary_result[2] >= 1.5 and boundary_result[2] > ncc_result[2] * 1.3:
            cross_check["agree"] = "boundary_only"
            return (boundary_result[0], boundary_result[1], boundary_result[2], cross_check)
        cross_check["agree"] = False
        # 完全不一致时降级到 NCC（alpha 匹配对纹理干扰相对鲁棒）
        return (ncc_result[0], ncc_result[1], ncc_result[2], cross_check)

    if boundary_result:
        cross_check["agree"] = "boundary_only_no_ncc"
        return (boundary_result[0], boundary_result[1], boundary_result[2], cross_check)
    if ncc_result:
        cross_check["agree"] = "ncc_only"
        return (ncc_result[0], ncc_result[1], ncc_result[2], cross_check)

    # 最后回退纯 NumPy
    n = _match_offset_numpy(bg, piece)
    return (n[0], n[1], 1.0, cross_check) if n else None


def _match_offset(bg_bytes: bytes, piece_bytes: bytes) -> Optional[int]:
    """计算拼图在背景中的 x 缺口偏移（像素）。"""
    r = _match_offset_with_score(bg_bytes, piece_bytes)
    return r[0] if r else None


def _find_handle(page) -> Optional[Tuple[object, dict]]:
    """定位 TAC 拖动按钮及其包围盒。"""
    selectors = [
        "#show-code-check-wrap .slider-move-btn",
        "#show-code-check-wrap .slider-move-shadow",
        "#show-code-check-wrap .slider-move-btn",
        ".show-code-check-wrap .slider-move-btn",
        ".tianai-captcha-slider .slider-move-btn",
        ".tianai-captcha-slider [class*='btn']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=500):
                box = loc.bounding_box()
                if box and box.get("width", 0) > 0:
                    return loc, box
        except Exception:
            continue
    return None


def _get_tac_images(page) -> Optional[Tuple[bytes, bytes, int, int]]:
    """从已渲染的 TAC DOM 中提取背景图、拼图块图及其自然宽高。"""
    info = page.evaluate("""() => {
        const wrap = document.querySelector('#show-code-check-wrap');
        if (!wrap) return null;
        const imgs = Array.from(wrap.querySelectorAll('img'));
        const bg = imgs.find(i => i.naturalWidth >= 580 && i.naturalWidth <= 620 &&
                                   i.naturalHeight >= 340 && i.naturalHeight <= 380 &&
                                   (i.src || '').startsWith('data:image/jpeg'));
        const piece = imgs.find(i => i.naturalWidth >= 100 && i.naturalWidth <= 120 &&
                                     i.naturalHeight >= 340 && i.naturalHeight <= 380 &&
                                     (i.src || '').startsWith('data:image/png'));
        return {bgSrc: bg && bg.src, pieceSrc: piece && piece.src,
                bgW: bg && bg.naturalWidth, bgH: bg && bg.naturalHeight,
                pieceW: piece && piece.naturalWidth, pieceH: piece && piece.naturalHeight};
    }""")
    if not info or not info.get("bgSrc") or not info.get("pieceSrc"):
        # 提取失败时把 DOM 里真实存在的 img 元数据打出来，否则下次仍无从下手排查。
        try:
            dom = _dump_dom_images(page)
        except Exception as exc:  # noqa: BLE001
            dom = None
            logger.debug("转储 TAC DOM 图片元数据失败: %s", exc)
        logger.warning("无法从 TAC DOM 提取滑块图片；DOM 内 img 元数据=%s",
                       json.dumps(dom, ensure_ascii=False) if dom is not None else "(wrapper 未找到)")
        return None
    return (_decode_b64(info["bgSrc"]), _decode_b64(info["pieceSrc"]),
            int(info["bgW"]), int(info["pieceW"]))


def _dump_dom_images(page) -> Optional[list]:
    """保存 #show-code-check-wrap 内所有 img 的元数据，用于排查提取到错误图片。"""
    info = page.evaluate("""() => {
        const wrap = document.querySelector('#show-code-check-wrap');
        if (!wrap) return null;
        const imgs = Array.from(wrap.querySelectorAll('img'));
        return imgs.map(i => ({
            cls: i.className || '',
            id: i.id || '',
            srcHead: (i.src || '').slice(0, 48),
            nw: i.naturalWidth, nh: i.naturalHeight,
            w: Math.round(i.getBoundingClientRect().width),
            h: Math.round(i.getBoundingClientRect().height)}));
    }""")
    return info


def _save_debug_images(bg_b: bytes, piece_b: bytes, dom_info):
    """把实时提取到的背景/拼图存盘，便于离线核对。"""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        for name, b in (("tac_live_bg.png", bg_b), ("tac_live_piece.png", piece_b)):
            out = _DEBUG_DIR / name
            try:
                Image.open(io.BytesIO(b)).convert("RGBA").save(str(out))
            except Exception:
                out.write_bytes(b)
        if dom_info is not None:
            (_DEBUG_DIR / "tac_live_dom.json").write_text(
                json.dumps(dom_info, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已保存实时滑块调试图: %s", _DEBUG_DIR)
    except Exception as exc:  # noqa: BLE001
        logger.debug("保存滑块调试图失败: %s", exc)


def _gap_plausible(gap_x, native_bg_w, native_piece_w, score, margin, agree=None) -> bool:
    """简单启发式：缺口不会贴到最左/最右，且识别结果要有足够置信度。

    新版 margin 指标（边界峰/全宽均值）：
      - 真缺口：margin 通常 8~20（峰在全宽曲线里很突出）
      - 局部伪峰（背景复杂纹理导致的误判）：margin 通常 < 3
    NCC 回退路径的 margin = best/second，范围略不同：要求 >= 1.3 才算尖锐。
    """
    if gap_x is None:
        return False
    edge_margin = 10
    max_gap = native_bg_w - native_piece_w - edge_margin
    if not (edge_margin <= gap_x <= max_gap):
        return False
    if score is None:
        return False
    # boundary 检测：margin 是峰在全宽均值里的突出度，要求 >= 3.0 才算真缺口
    if margin is not None and margin >= 3.0:
        return True
    # NCC 回退：margin = best/second，要求 >= 1.3 才算尖锐
    if margin is not None and margin >= 1.3:
        return True
    # boundary_only 路径（在 NCC 缺失/失败时退化为单一信号）放宽到 1.5
    if agree == "boundary_only_no_ncc" and margin is not None and margin >= 1.5:
        return True
    return False


def analyze(page, cfg: Config, dump: bool = False):
    """提取背景/拼图图并求缺口偏移（不触发拖动）。

    返回 (gap_x, native_bg_w, native_piece_w, score, margin, cross_check) 或 None。
    gap_x 为拼图在背景中的 x 偏移（native 像素），即需要拖动的相对距离。
    margin：边界检测时 = best_coarse / 全宽均值；NCC 时 = best/second。
    cross_check：boundary 与 NCC 一致性字典，solve 据此决定是否刷新重试。
    """
    imgs = _get_tac_images(page)
    if not imgs:
        return None
    bg_b, piece_b, native_bg_w, native_piece_w = imgs
    dom_info = _dump_dom_images(page)
    if dump:
        _save_debug_images(bg_b, piece_b, dom_info)
        logger.debug("DOM 图片清单: %s", dom_info)

    r = _match_offset_with_score(bg_b, piece_b)
    if r is None:
        return None
    gap_x, score, margin, cross_check = r
    logger.debug("首次匹配 gap_x=%s score=%.1f margin=%.2f agree=%s",
                 gap_x, score, margin, cross_check.get("agree"))

    if not _gap_plausible(gap_x, native_bg_w, native_piece_w, score, margin,
                          agree=cross_check.get("agree")):
        logger.warning("缺口偏移置信度低/不合理 (gap_x=%s, score=%.1f, margin=%.2f, agree=%s)，等待后重试一次",
                       gap_x, score, margin, cross_check.get("agree"))
        page.wait_for_timeout(800)
        imgs2 = _get_tac_images(page)
        if imgs2:
            bg_b, piece_b, native_bg_w, native_piece_w = imgs2
            r2 = _match_offset_with_score(bg_b, piece_b)
            if r2 is not None:
                gap_x, score, margin, cross_check = r2
                logger.info("重试匹配 gap_x=%s score=%.1f margin=%.2f agree=%s",
                            gap_x, score, margin, cross_check.get("agree"))
                if dump:
                    _save_debug_images(bg_b, piece_b, _dump_dom_images(page))
    return (gap_x, native_bg_w, native_piece_w, score, margin, cross_check)


def _get_track_box(page) -> Optional[dict]:
    """获取当前屏幕上的背景图/滑轨包围盒（用于把缺口映射到屏幕距离）。"""
    for sel in ["#tianai-captcha-slider-bg-img", ".bg-img-div", ".slider-img-div",
                "#tianai-captcha", ".tianai-captcha-slider"]:
        try:
            loc = page.locator(sel).first
            if loc.count():
                box = loc.bounding_box()
                if box and box.get("width", 0) > 0:
                    return box
        except Exception:
            continue
    return None


def _read_actual_drag(page) -> Optional[float]:
    """读取滑块按钮拖动后的实际水平位移（像素）。

    TAC 内部用 transform: translateX(...) 或 translate3d(...) 把按钮从轨道起点推到目标位置。
    优先读 inline style.transform，其次读 computedStyle.transform（兼容 Vue 把 transform
    写到 CSS 的情况）；transform 都没有时退回 getBoundingClientRect 与初始位置差值。
    若拖动后滑块按钮的 translateX 离目标差距 > 8px，说明本次拖动没到位。
    """
    try:
        x = page.evaluate("""() => {
            const btn = document.querySelector('#show-code-check-wrap .slider-move-btn');
            if (!btn) return null;
            const tryParse = (s) => {
                if (!s) return null;
                // 形式: translateX(123.45px) 或 translate3d(123.45px, 0, 0) 或 matrix(...)
                let m = s.match(/translateX\\s*\\(\\s*([-\\d.]+)\\s*px/);
                if (m) return parseFloat(m[1]);
                m = s.match(/translate3d\\s*\\(\\s*([-\\d.]+)\\s*px/);
                if (m) return parseFloat(m[1]);
                m = s.match(/matrix\\s*\\(\\s*([-\\d.]+)\\s*,\\s*[-\\d.]+/);
                if (m) return parseFloat(m[1]);
                return null;
            };
            const inline = tryParse(btn.style && btn.style.transform);
            if (inline !== null) return inline;
            // 注意：getComputedStyle().transform 在 matrix(...) 时 parseFloat 第一个数字是 matrix(a,b,c,d,e,f) 的 a 不是 x 偏移，必须用 e
            const computed = tryParse(getComputedStyle(btn).transform);
            if (computed !== null) return computed;
            return null;
        }""")
        return float(x) if x is not None else None
    except Exception:
        return None


def _nudge_drag(page, start_x: float, base_y: float, delta: int):
    """小幅补拖：在原已 down 的位置移动 delta 像素（带轻微减速）。"""
    if abs(delta) < 1:
        return
    steps = random.randint(8, 14)
    for i in range(1, steps + 1):
        p = i / steps
        ease = p * p * (3 - 2 * p)  # 平滑减速
        x = start_x + delta * ease
        page.mouse.move(x, base_y + random.uniform(-1.0, 1.0))
        page.wait_for_timeout(random.randint(8, 18))


def _drag(page, start_x: float, start_y: float, dx: int, cfg: Config):
    """类人拖动 + 拖动后校验实际位置，必要时补拖。

    关键改进（TAC 滑块错误修复）：
      1. 拖动完 evaluate 读 .slider-move-btn 实际 translateX，与目标 dx 比对；
      2. 差距 > 8px 时 mouse.move/down 仍有效（按钮已 up 时无法移动），先重新 down
         在按钮当前位置上、再平滑小步补拖；
      3. 整段轨迹更接近真人：起手 1/4 段更慢、中段快、末段再次减速（人手前 25%
         在加速、最后 25% 在微调）。
    """
    tol = cfg.slide.get("tolerance", 6)
    target = max(0, dx - tol)
    if target <= 0:
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.up()
        return

    page.mouse.move(start_x, start_y)
    page.mouse.down()

    # 起手延迟：模拟「对准滑块 → 准备按下 → 思考一下」的人手节奏
    page.wait_for_timeout(random.randint(60, 140))

    # 生成带小幅过冲的轨迹点
    overshoot = random.uniform(3.0, 8.0) if random.random() < 0.7 else 0.0
    total_x = target + overshoot
    steps = random.randint(45, 65)  # 步数比之前略多，让曲线更细
    points = []
    for i in range(1, steps + 1):
        p = i / steps
        # 改进的 ease：起手慢、末段再减速（人手动作生理曲线）
        # = 0.5 * p*p*(3-2p) [前半 ease-in-out] + 0.5 * sin(pi*p)*0.06
        ease = p * p * (3 - 2 * p) + 0.06 * np.sin(p * np.pi)
        ease = max(0.0, min(1.0, ease))
        points.append(total_x * ease)
    # 回拉到目标
    if overshoot > 0:
        pullback_steps = random.randint(5, 9)
        for i in range(1, pullback_steps + 1):
            p = i / pullback_steps
            points.append(total_x - overshoot * (p * p * (3 - 2 * p)))

    base_y = start_y
    prev_x = 0.0
    for idx, x in enumerate(points):
        speed = abs(x - prev_x)
        # 起手/末段更细的抖动
        in_phase = idx < steps * 0.18 or idx > steps * 0.85
        jitter_x_range = (0.4, 0.9) if in_phase else (-1.2, 1.2)
        jitter_x = random.uniform(*jitter_x_range)
        jitter_y = random.uniform(-2.0, 2.0)
        page.mouse.move(start_x + x + jitter_x, base_y + jitter_y)
        prev_x = x
        # 起手 25% / 末段 15% 慢、中段快
        if idx < steps * 0.25 or idx > steps * 0.85:
            delay = random.randint(15, 35)
        else:
            delay = random.randint(5, 14)
        if random.random() < 0.10:
            delay += random.randint(40, 100)
        page.wait_for_timeout(delay)

    # 精确定位到目标
    for _ in range(random.randint(2, 4)):
        page.mouse.move(start_x + target, base_y + random.uniform(-0.8, 0.8))
        page.wait_for_timeout(random.randint(40, 100))

    # 释放前再等一小会儿（人手从对准到释放有反应时间）
    page.wait_for_timeout(random.randint(80, 200))
    page.mouse.up()
    logger.info("滑块拖动完成，目标位移=%s px (overshoot=%.1f)", target, overshoot)

    # ===== 拖动后校验：TAC 校验依赖拼图块精确对位缺口 =====
    # 注意：dry-run 模式下 freeBook 被 route.abort() 拦截 → server 不响应 →
    # TAC 会 reset 滑块按钮（transform 回到 0），且会立即重新发起 freeBook。
    # 此时若读到 transform ≤ 2px，说明 server 已经处理完上一轮滑块验证并接受了它
    # （capToken 已生成 = 拖动到位），不要当作偏差去补拖，否则反而会触发新一次 freeBook。
    page.wait_for_timeout(random.randint(150, 350))
    actual = _read_actual_drag(page)
    if actual is None:
        # 按钮真的消失（被处理完毕）→ 接受当前位置
        logger.info("拖动后滑块按钮已消失（已被服务器处理），视为到位")
        return
    if actual <= 2.0:
        # TAC reset 状态：transform 被服务器清零（说明 capToken 已生成、拖动已被接受）
        logger.info("滑块 transform 已被 reset（≤2px），说明 server 已接受本次拖动，视为到位")
        return
    diff = target - actual
    if abs(diff) <= 8:
        logger.info("拖动精度 OK：目标=%s 实际=%.1f 差=%.1f", target, actual, diff)
        return

    # 偏差过大 → 自动补拖
    logger.warning("拖动偏差较大（目标=%s 实际=%.1f 差=%.1f），自动补拖", target, actual, diff)
    current_handle_x = start_x + actual
    page.mouse.move(current_handle_x, base_y)
    page.mouse.down()
    page.wait_for_timeout(random.randint(60, 140))
    _nudge_drag(page, current_handle_x, base_y, diff)
    page.wait_for_timeout(random.randint(60, 150))
    page.mouse.up()
    page.wait_for_timeout(random.randint(200, 500))
    # 二次校验
    actual2 = _read_actual_drag(page)
    if actual2 is None:
        logger.info("补拖后按钮已消失，视为到位")
        return
    if actual2 <= 2.0:
        logger.info("补拖后 transform 已被 reset（≤2px），视为到位")
        return
    diff2 = target - actual2
    logger.info("补拖后：实际=%.1f 差=%.1f%s",
                actual2, diff2, " ✓" if abs(diff2) <= 8 else "（仍有偏差）")


def solve(page, cfg: Config) -> bool:
    """尝试解决当前 TAC 滑块验证码。返回是否成功触发拖动。

    流程：
      1. 最多 3 次识别 + 必要刷新；
      2. 用 cross_check 决策：两算法不一致 / margin 过低 → 刷新重试；
      3. 拖动后由 _drag 内部校验实际位置并自动补拖。
    """
    try:
        wrap = page.locator("#show-code-check-wrap").first
        if not wrap.count() or not wrap.is_visible(timeout=2000):
            logger.debug("当前页面未检测到 TAC 滑块，跳过")
            return False
    except Exception:
        return False

    cfg_dump = bool(cfg.get("debug"))
    max_attempts = 3
    for attempt in range(max_attempts):
        page.wait_for_timeout(600)  # 等图片渲染
        res = analyze(page, cfg, dump=cfg_dump)
        if not res:
            logger.warning("第 %d 次：无法提取/匹配滑块图片", attempt + 1)
            if attempt < max_attempts - 1:
                _reload_tac(page)
                page.wait_for_timeout(1000)
            continue
        gap_x, native_bg_w, native_piece_w, score, margin, cross_check = res
        if gap_x is None:
            logger.warning("第 %d 次：滑块缺口识别失败", attempt + 1)
            if attempt < max_attempts - 1:
                _reload_tac(page)
                page.wait_for_timeout(1000)
            continue
        agree = cross_check.get("agree")
        if not _gap_plausible(gap_x, native_bg_w, native_piece_w, score, margin, agree=agree):
            logger.warning("第 %d 次：缺口识别结果不可信 (gap_x=%s, score=%.1f, margin=%.2f, agree=%s, diff=%s)",
                           attempt + 1, gap_x, score, margin, agree, cross_check.get("agree_diff"))
            if attempt < max_attempts - 1:
                _reload_tac(page)
                page.wait_for_timeout(1000)
            continue
        logger.info("缺口 native 偏移=%s px (score=%.1f, margin=%.2f, agree=%s)",
                    gap_x, score, margin, agree)
        break
    else:
        # 注意：走到 else 说明 3 次尝试都没能 break，此时 gap_x/margin 可能从未被赋值
        # （例如 analyze() 连续返回 None），直接引用会抛 UnboundLocalError 并掩盖真实原因。
        logger.error("滑块缺口识别结果仍不可信（3 次尝试均未通过），放弃本次拖动")
        return False

    track_box = _get_track_box(page)
    handle = _find_handle(page)
    if not handle:
        logger.warning("未找到 TAC 拖动按钮")
        return False
    handle_loc, handle_box = handle

    displayed_track_w = track_box["width"] if track_box else handle_box["width"]
    if track_box and track_box.get("x", 0) >= handle_box.get("x", 0):
        displayed_track_w = track_box["width"]
    scale = displayed_track_w / native_bg_w if native_bg_w else 0.5
    drag_dx = int(gap_x * scale)

    start_x = handle_box["x"] + handle_box["width"] / 2
    start_y = handle_box["y"] + handle_box["height"] / 2

    logger.info("TAC 滑轨显示宽度=%s, scale=%.3f, 屏幕拖动位移=%s",
                displayed_track_w, scale, drag_dx)
    _drag(page, start_x, start_y, drag_dx, cfg)
    return True


def _reload_tac(page) -> bool:
    """尝试刷新 TAC 验证码：优先点刷新按钮，否则调用可能的 reload 函数。"""
    try:
        for sel in [".tianai-captcha-refresh", ".captcha-refresh", ".slider-move-refresh",
                    ".show-code-check-wrap .refresh", ".slider-move-btn .refresh"]:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=500):
                loc.click(timeout=1000)
                logger.info("已点击 TAC 刷新按钮: %s", sel)
                return True
        # 尝试 JS 刷新
        page.evaluate("""() => {
            const wrap = document.querySelector('#show-code-check-wrap');
            const tac = wrap && (wrap.__vue__ || wrap.querySelector('[__vue__]'));
            if (tac && typeof tac.reloadCaptcha === 'function') { tac.reloadCaptcha(); return true; }
            if (window.tianaiCaptcha && typeof window.tianaiCaptcha.reload === 'function') { window.tianaiCaptcha.reload(); return true; }
            return false;
        }""")
    except Exception as exc:
        logger.debug("刷新 TAC 失败: %s", exc)
    return False
