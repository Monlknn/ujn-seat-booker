"""在页面内查找可复用的 HTTP 客户端（带 HMAC 拦截器），以便从 page.evaluate 调用 lastMake 校验预约。"""
from src.config import load_config
from src.session import login_browser


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

        info = page.evaluate("""() => {
            const out = {};
            // 1) window 全局里可能暴露的 http 客户端
            const cand = ['axios','$http','http','request','service','api','$request','$axios','Vue'];
            out.winKeys = cand.filter(k => k in window);
            // 2) Vue 根实例上的原型方法
            const app = document.getElementById('app');
            const root = app && app.__vue__;
            if (root) {
                const proto = Object.getPrototypeOf(root);
                out.rootProtoMethods = Object.getOwnPropertyNames(proto).filter(m => /http|request|axios|\$/.test(m));
                out.rootHasDollarHttp = !!root.$http;
            }
            // 3) 尝试通过常见客户端直接调用 lastMake
            const tries = {};
            function callClient(client, name) {
                try {
                    const p = client.get ? client.get('/user/lastMake') : null;
                    if (p && p.then) {
                        return p.then(r => r.data).catch(e => ({err: String(e)}));
                    }
                } catch(e) { return {err: String(e)}; }
                return null;
            }
            if (window.axios) tries.axios = callClient(window.axios, 'axios');
            if (root && root.$http) tries.rootHttp = callClient(root.$http, '$http');
            out.tries = tries;
            return out;
        }""")
        print("winKeys:", info.get("winKeys"))
        print("rootProtoMethods:", info.get("rootProtoMethods"))
        print("rootHasDollarHttp:", info.get("rootHasDollarHttp"))
        print("tries:", info.get("tries"))

        # 等待上面的 promise 结果（如果有的话）再读一次
        page.wait_for_timeout(2000)
        info2 = page.evaluate("""() => {
            const app = document.getElementById('app');
            const root = app && app.__vue__;
            const res = {};
            // 递归在 root 及其子找带 request/get 的对象（可能是 http 实例）
            function walk(vm, depth){
                if (!vm || depth>6) return;
                for (const k of Object.keys(vm)) {
                    const v = vm[k];
                    if (v && typeof v === 'object' && (v.get && v.post) && !res.found) {
                        try { res.found = k; res.sample = Object.keys(v).slice(0,20);} catch(e){}
                    }
                }
                for (const c of (vm.$children||[])) walk(c, depth+1);
            }
            walk(root, 0);
            return res;
        }""")
        print("vm 内 http 实例搜索:", info2)
    finally:
        sess.close()


if __name__ == "__main__":
    main()
