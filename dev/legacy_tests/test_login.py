"""实测：用浏览器完成 SSO 登录，验证登录态，并探查预约/滑块相关 DOM。不提交任何预约。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config
from src.session import login_browser
from src.logger import logger  # noqa: F401

cfg = Config.load(Path(__file__).resolve().parents[2] / "config.json")

# 用有头模式 + debug 便于观察
sess = login_browser(cfg, headless=False)
page = sess.page

print("== 登录后 URL ==", page.url)

# 登录诊断：若仍停在 SSO 页，抓取页面错误提示
if "sso.ujn.edu.cn" in page.url:
    print("== 登录未跳转，诊断页面 ==")
    # 常见错误提示容器
    for sel in [".error", ".msg", ".login-tip", ".warning", "#msg", ".layui-layer-content"]:
        try:
            el = page.locator(sel).first
            if el.count():
                print(f"  {sel}: {el.inner_text()!r}")
        except Exception:
            pass
    # 直接评估按钮状态并尝试 JS 触发
    try:
        info = page.evaluate("""() => {
            const btn = document.querySelector('#index_login_btn');
            if (!btn) return {found:false};
            const r = btn.getBoundingClientRect();
            return {found:true, visible: r.width>0 && r.height>0,
                    disabled: btn.disabled, rect:{w:r.width,h:r.height}};
        }""")
        print("  btn info:", info)
        # 读实际输入值和隐藏字段
        fields = page.evaluate("""() => {
            return {
                un: document.querySelector('input#un')?.value || '',
                pd_len: document.querySelector('input#pd')?.value?.length || 0,
                rsa: document.querySelector('input#rsa')?.value || '',
                lt: document.querySelector('input#lt')?.value || '',
                execution: document.querySelector('input[name=execution]')?.value || '',
                ul: document.querySelector('input#ul')?.value || '',
                pl: document.querySelector('input#pl')?.value || ''
            };
        }""")
        print("  form fields:", fields)
        # 强制调用页面 login()（若全局暴露）
        called = page.evaluate("""() => {
            try { if (typeof login === 'function'){ login(); return 'login() invoked'; } }
            catch(e){ return 'login err: '+e.message; }
            return 'no global login()';
        }""")
        print("  login() invoke:", called)
        page.wait_for_timeout(3000)
        print("  after login() URL:", page.url)
    except Exception as exc:
        print("  eval err:", exc)
    try:
        page.screenshot(path=str(Path(__file__).resolve().parents[2] / "debug" / "login_diag.png"))
        print("  已保存诊断截图 debug/login_diag.png")
    except Exception as exc:
        print("  截图失败:", exc)
# 检查是否真的登录（尝试访问 jsq-v 首页或带用户态的接口）
try:
    # 观察页面里是否有用户/登出/预约入口
    txt = page.content()
    for kw in ["退出", "登出", "logout", "预约", "选座", "我的", "欢迎"]:
        print(f"  页面含 [{kw}]:", kw in txt)
    # 尝试访问预约主页（带登录态 cookie 由浏览器自动带上）
    page.goto("https://seat.ujn.edu.cn/jsq-v", wait_until="domcontentloaded", timeout=20000)
    print("== 进入 jsq-v URL ==", page.url)
    print("jsq-v 标题:", page.title())
    # 抓页面里所有可点击/可识别的预约相关元素文本
    content = page.content()
    import re
    # 粗略提取含 '预约'/'座位'/'阅览室' 的文本片段
    snippets = re.findall(r'.{0,15}(?:预约|座位|阅览室|选座).{0,15}', content)
    print("预约相关片段样例:")
    for s in snippets[:20]:
        print("   ", s.strip())
except Exception as exc:
    print("探查异常:", exc)
finally:
    sess.close()
