"""捕获前端调用 frontApi 时实际发出的请求头，确定鉴权字段。"""
from src.config import load_config
from src.session import login_browser


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        reqs = []

        def _on_req(request):
            u = request.url
            if "frontApi" in u:
                reqs.append((u, dict(request.headers)))

        page.on("request", _on_req)
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(4000)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
        # 触发一次可能的接口调用：点开"记录查询"
        for kw in ("记录查询", "我的预约", "今日预约"):
            try:
                loc = page.get_by_text(kw, exact=False).first
                if loc.count():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
        page.wait_for_timeout(1500)

        print(f"捕获到 {len(reqs)} 个 frontApi 请求，lastMake 请求的完整头部：")
        lastmake = [r for r in reqs if "lastMake" in r[0]]
        target = lastmake[0] if lastmake else (reqs[0] if reqs else None)
        if target:
            u, h = target
            print("URL:", u)
            for k, v in h.items():
                print(f"  {k}: {v[:120]}")
        else:
            print("未捕获到 lastMake 请求")
    finally:
        sess.close()


if __name__ == "__main__":
    main()
