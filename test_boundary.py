"""测试生产环境 _match_offset_boundary / _match_offset_with_score。"""
from pathlib import Path
from src.slider import _load_image, _match_offset_with_score

DBG = Path("debug")
bg_b = (DBG / "tac_live_bg.png").read_bytes()
piece_b = (DBG / "tac_live_piece.png").read_bytes()
bg = _load_image(bg_b)
piece = _load_image(piece_b)
print(f"bg {bg.shape} piece {piece.shape}")

r = _match_offset_with_score(bg_b, piece_b)
if r:
    gap_x, score, margin = r
    print(f"gap_x={gap_x} score={score:.1f} margin={margin:.3f}")
else:
    print("None")
