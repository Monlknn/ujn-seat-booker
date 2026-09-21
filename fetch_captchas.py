"""登录后批量获取 TAC 滑块验证码样本，用于离线测试缺口检测算法。

1. 用浏览器登录并进入预约系统，获取 token（写入 sessionStorage）。
2. 用该 token POST gen/SLIDER API 下载背景图/拼图。
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.config import load_config
from src import session as br

OUT = Path("debug/captchas")
OUT.mkdir(parents=True, exist_ok=True)
URL = "https://seat.ujn.edu.cn/jsq/static/frontApi/static/cap/cg/gen/SLIDER"


def save_b64(base, field, suffix, cap):
    src = cap.get(field)
    if not src:
        return None
    b64 = re.sub(r"^data:image/[^;]+;base64,", "", src)
    img = base64.b64decode(b64)
    path = base.with_name(f"{base.name}_{suffix}").with_suffix(".png")
    path.write_bytes(img)
    return path


def fetch_one(page, idx: int) -> bool:
    username = f"test_{uuid.uuid4().hex[:8]}"
    try:
        r = page.request.post(URL, headers={"Content-Type": "application/json"},
                              data=json.dumps({"username": username}, ensure_ascii=False))
        data = r.json()
        if not data.get("status"):
            print(f"{idx}: gen failed {data.get('message')}")
            return False
        cap = data.get("captcha", data)
        base = OUT / f"cap_{idx:03d}"
        (base.with_suffix(".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        p_bg = save_b64(base, "backgroundImage", "bg", cap)
        p_pc = save_b64(base, "templateImage", "piece", cap)
        print(f"{idx}: ok bg={p_bg.name if p_bg else None} piece={p_pc.name if p_pc else None}")
        return True
    except Exception as exc:
        print(f"{idx}: error {exc}")
        return False


def main():
    cfg = load_config()
    sess = br.login_browser(cfg)
    try:
        page = sess.page
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        info = page.evaluate("""() => {
            const keys = [];
            for (let i=0;i<sessionStorage.length;i++) keys.push(sessionStorage.key(i));
            const token = sessionStorage.getItem('token') || sessionStorage.getItem('jsq_p-token');
            return {keys, token, url: location.href};
        }""")
        print("sessionStorage keys:", info.get("keys"))
        print("token:", (info.get("token") or "")[:8] + "..." if info.get("token") else "NONE")
        token = info.get("token") or ""
        if not token:
            print("无法获取 token")
            return
        ok = 0
        for i in range(8):
            if fetch_one(page, i):
                ok += 1
            time.sleep(0.6)
        print(f"done {ok}/8")
    finally:
        sess.close()


if __name__ == "__main__":
    main()
