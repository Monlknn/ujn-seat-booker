"""确认 frontApi 的鉴权方式，便于在 booker 里用 lastMake 校验预约是否真的生成。"""
from src.config import load_config
from src.session import login_browser

LASTMAKE = "https://seat.ujn.edu.cn/jsq/static/frontApi/user/lastMake"


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        token = page.evaluate("() => sessionStorage.getItem('jsq_p-token') || ''")
        print("token 长度:", len(token) if token else 0)

        # 方式 a: cookies only
        r_a = page.request.get(LASTMAKE)
        print("a) cookies-only:", r_a.status, _safe(r_a))

        # 方式 b: token 参数
        r_b = page.request.get(LASTMAKE, params={"token": token})
        print("b) token 参数:", r_b.status, _safe(r_b))

        # 方式 d: token 请求头（已确认 app 用此方式鉴权）
        r_d = page.request.get(LASTMAKE, headers={"token": token})
        print("d) token 头部:", r_d.status, _safe(r_d))

        # 方式 c: Authorization Bearer + token header (page.evaluate fetch)
        r_c = page.evaluate("""(tk) => fetch('__URL__', {
            headers: {'Authorization': 'Bearer ' + tk, 'token': tk, 'Content-Type':'application/json'}
        }).then(r=>r.json()).catch(e=>({error:String(e)}))""".replace("__URL__", LASTMAKE), token)
        print("c) Bearer header:", r_c)
    finally:
        sess.close()


def _safe(resp):
    try:
        j = resp.json()
        data = j.get("data")
        if isinstance(data, list):
            return f"status={j.get('status')} 条数={len(data)} 首条={(data[0] if data else None)}"
        return str(j)[:200]
    except Exception:
        try:
            return resp.text()[:200]
        except Exception:
            return "?"


if __name__ == "__main__":
    main()
