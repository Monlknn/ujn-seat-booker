"""对刚才 probe_slider 保存的实时图片做一次离线复核，
并生成 piece 叠加到 bg 缺口位置的可视化验证图。"""
from __future__ import annotations

from pathlib import Path
import numpy as np
from PIL import Image

from src.slider import _match_offset

DBG = Path(__file__).resolve().parents[2] / "debug"
bg_b = (DBG / "tac_live_bg.png").read_bytes()
piece_b = (DBG / "tac_live_piece.png").read_bytes()

offset = _match_offset(bg_b, piece_b)
print("live gap_x:", offset, "fraction:", offset / 600 if offset else None)

# 可视化：把 piece 叠加到 bg 的 offset 处，验证位置
bg = Image.open(DBG / "tac_live_bg.png").convert("RGBA")
piece = Image.open(DBG / "tac_live_piece.png").convert("RGBA")
overlay = bg.copy()
overlay.paste(piece, (offset, 0), piece)
overlay.save(DBG / "tac_live_overlay.png")
print("saved overlay:", DBG / "tac_live_overlay.png")
