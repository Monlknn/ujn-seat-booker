"""SSO 登录与浏览器会话模块。

济大的座位系统登录走统一身份认证（sso.ujn.edu.cn，CAS 风格 tpass），登录本身
【不需要验证码】，纯账号+密码即可。
注意：老的 rest/auth 直连接口现已失效（返回 code:31 验证码错误），因此登录必须走
真实浏览器 SSO 流程。

登录页关键机制：
  - 可见输入框 #un/#pd 仅供用户输入，表单实际提交的是隐藏字段：
      rsa = 3DES(strEnc(账号 + 密码 + lt, '1', '2', '3'))
      ul  = 账号长度
      pl  = 密码长度
      lt / execution / _eventId
  - 直接用 `document.getElementById('loginForm').submit()` 并填充好隐藏字段即可登录，
    无需操作可见输入框（避免模板重复 ID 导致 fill 失败的问题）。
登录成功后浏览器跳回 seat.ujn.edu.cn 已登录态，届时预约提交环节出现滑块验证码。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .config import Config, PROJECT_ROOT
from .logger import logger

SEAT_URL = "https://seat.ujn.edu.cn/"
HOME_URL = "https://seat.ujn.edu.cn/jsq-v/#/main/home"
SSO_URL = ("https://sso.ujn.edu.cn/tpass/login?service="
           "https%3A%2F%2Fseat.ujn.edu.cn%2Frem%2Fstatic%2Fsso%2FwebOAuthRed")

# 会话缓存目录：复用登录态可省去每次约 10 秒的 SSO 登录
SESSION_DIR = PROJECT_ROOT / ".session"
STATE_FILE = SESSION_DIR / "storage_state.json"   # cookie + localStorage
SS_FILE = SESSION_DIR / "session_storage.json"    # sessionStorage（登录 token 在这里）
SESSION_TTL_SECONDS = 6 * 3600

# 登录态 token 在 sessionStorage 的键名
TOKEN_KEY = "jsq_p-token"


class BrowserSession:
    """封装 Playwright 浏览器会话，负责 SSO 登录与后续页面操作。"""

    def __init__(self, cfg: Config, headless: Optional[bool] = None):
        self.cfg = cfg
        self.page = None
        self.context = None
        self.browser = None
        self._pw = None
        self._headless = cfg.get("headless", True) if headless is None else headless
        # 默认用本机已安装的 Edge（Chromium 内核），避免下载 Chromium
        self._channel = cfg.get("browser_channel", "msedge")

    def _import_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
            return sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "未安装 playwright，请先执行: pip install playwright"
            ) from exc

    # ---------------- 会话缓存：复用登录态，跳过 SSO ----------------
    @staticmethod
    def _cache_fresh() -> bool:
        """cookie 缓存是否在有效期内。

        cookie（CASTGC / JSESSIONID）才是 CAS 真正的登录凭据，sessionStorage 里
        的 token 要等 Vue 应用启动后才有，所以这里只以 cookie 文件为准。
        """
        try:
            if not STATE_FILE.exists():
                return False
            return (time.time() - STATE_FILE.stat().st_mtime) < SESSION_TTL_SECONDS
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _load_session_storage():
        """读取缓存的 sessionStorage，返回可直接内嵌进 JS 的 JSON 文本。

        注意 ensure_ascii=True：这段 JSON 会被拼进 init script 源码，
        全 ASCII 最不容易出编码问题（JS 会把 \\uXXXX 正常解回中文）。

        【关键】丢弃 `jsq_p-token`：前端启动时若发现 sessionStorage 已有 token
        会**跳过换取流程**，直接拿我们注入的过期 token 去调业务接口，
        服务端返回 "token 访问过期"。所以只注入无业务依赖的字段（userInfo、
        systemInfo 等），token 让前端自然生成。
        """
        try:
            if not SS_FILE.exists():
                return None
            data = json.loads(SS_FILE.read_text(encoding="utf-8"))
            # 业务凭据必须重新生成，不能复用
            data.pop("jsq_p-token", None)
            return json.dumps(data, ensure_ascii=True) if data else None
        except Exception:  # noqa: BLE001
            return None

    def _save_session_cache(self):
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            self.context.storage_state(path=str(STATE_FILE))
            # sessionStorage 只有在 Vue 应用启动后才装入 token；
            # 为空时不要拿空对象覆盖掉上一次存好的那份。
            ss = self.page.evaluate("() => JSON.stringify(sessionStorage)")
            if ss and ss != "{}":
                SS_FILE.write_text(ss, encoding="utf-8")
            logger.debug("已缓存登录态 -> %s", SESSION_DIR)
        except Exception as exc:  # noqa: BLE001
            logger.debug("缓存登录态失败: %s", exc)

    def _is_logged_in(self, page) -> bool:
        """带缓存访问首页，判断登录态是否可用。

        坑点：cookie 有效时座位应用仍会先跳 SSO 换一个 JWT 再跳回（有 CAS cookie
        时这一来一回是瞬时的）。因此不能「一看到 sso 域名就判失败」，而应等到
        「token 落位」或「首页控件挂载」任一成立，才算登录态可用。

        加速策略：cookie fresh 时，3 秒后还没看到房间卡片（.room.el-col）就主动
        probe 一次 token（fetch roomListByCondition 看业务接口有没有真返回数据），
        命中即视为已登录。这能把「Vue 启动慢 + 房间卡片数据还没拿到」的等待
        从 12 秒压到 ~4 秒。

        ★ 2026-09-04 修正：济大 SPA **不用 .el-card**，房间卡片 class 是
        .room.el-col.el-col-24。改用它做「首页渲染就绪」硬证据。
        """
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:  # noqa: BLE001
            return False
        # cookie fresh 时 6 秒已足够；不 fresh 时给 8 秒兜底（此时基本会落到 SSO 表单）。
        cache_fresh = self._cache_fresh()
        deadline = time.monotonic() + (6.0 if cache_fresh else 8.0)
        probe_done = False
        # 3 秒后开始主动 probe token（仅 cookie fresh 时）
        probe_after = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                st = page.evaluate("""(k) => ({
                    tok: !!sessionStorage.getItem(k),
                    // 硬证据：服务端真的返回了阅览室数据。
                    // 注意两个陷阱——
                    //   1) sessionStorage 里的 token 是我们自己注入的，过期了也存在；
                    //   2) 未登录时 Vue 照样把首页壳子和 el-select 渲染出来（URL 也正常），
                    //      只有接口静默失败、.room.el-col 一个都没有。
                    // 所以只能拿「服务端数据是否到位」当判据。
                    roomData: document.querySelectorAll('.room.el-col').length > 0,
                    // 停在 SSO 的登录表单上 = 确实没登录
                    ssoForm: !!(document.getElementById('loginForm')
                                || document.querySelector("input[name='lt']")),
                    host: location.hostname || ''
                })""", TOKEN_KEY)
            except Exception:  # noqa: BLE001
                # 跳转途中的 "Execution context was destroyed"，忽略继续等
                st = None
            if st:
                if st.get("roomData"):
                    return True
                # 已经掉到登录页就不要再傻等（此前会白等满 12 秒才去重新登录）
                if st.get("ssoForm") and "sso.ujn.edu.cn" in st.get("host", ""):
                    logger.debug("缓存登录态已失效，立即回退到完整 SSO 登录")
                    return False
            # cookie fresh + 3 秒还没 .room.el-col → 主动 probe token
            # 命中即视为已登录，省去剩余轮询时间。
            if cache_fresh and not probe_done and time.monotonic() >= probe_after:
                if self._probe_token_valid(page):
                    return True
                probe_done = True
            page.wait_for_timeout(200)
        return False

    def open(self):
        """启动浏览器并拿到已登录 page。

        快路径：会话缓存有效 → 直接复用（跳过 SSO，省约 10 秒）。
        慢路径：完整 SSO 登录，成功后写回缓存供下次复用。
        """
        sync_playwright = self._import_playwright()
        pw = sync_playwright().start()
        self._pw = pw
        launch_kwargs = {"headless": self._headless}
        if self._channel:
            launch_kwargs["channel"] = self._channel
        self.browser = pw.chromium.launch(**launch_kwargs)

        ctx_kwargs = {
            "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "viewport": {"width": 1280, "height": 900},
        }
        if STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(STATE_FILE)
        self.context = self.browser.new_context(**ctx_kwargs)

        # sessionStorage 不在 storage_state 里，需自行恢复（登录 token 就存在这儿）。
        # 坑：较旧版 playwright 的 add_init_script 没有 arg 参数
        # （签名是 add_init_script(script=None, *, path=None)），按位置或关键字传 arg 都会报
        # "takes from 1 to 2 positional arguments"。改为把 JSON 直接内嵌进脚本，新旧版本通用。
        ss = self._load_session_storage()
        if ss:
            self.context.add_init_script(
                "(function(){try{var d=" + ss + ";"
                "for(var k in d){sessionStorage.setItem(k,d[k]);}}catch(e){}})()")

        self.page = self.context.new_page()
        page = self.page

        # 缓存有效就复用：cookie 足以通过 CAS；sessionStorage 里有 token 则连换取都省了
        if self._cache_fresh() and self._is_logged_in(page):
            logger.info("复用缓存登录态，跳过 SSO 登录")
            self._save_session_cache()   # 此时应用已启动，token 已落位，正好存下来
            return page

        # cache_fresh=True 但 is_logged_in 失败的情况：
        # 通常是 cookie 还有效（首页能打开）但 sessionStorage 里 token 没注入成功，
        # 导致 .room.el-col > 0 等不到。这时直接 probe token 探测一下，
        # 有效就当作已登录、跳过 12s 轮询 + SSO 跳转，能省 ~10 秒。
        if self._cache_fresh() and self._probe_token_valid(page):
            logger.info("检测到已登录态（cookie + token 均有效）")
            self._save_session_cache()
            return page

        # 走到这里说明缓存不被服务端认可（token 过期等）。必须丢掉旧 token：
        # 此刻应用还没启动、sessionStorage 是空的，_save_session_cache 不会覆盖它，
        # 否则这个坏 token 会一直被注入，把快路径永久带偏。
        if SS_FILE.exists():
            try:
                SS_FILE.unlink()
                logger.debug("已丢弃失效的 sessionStorage 缓存")
            except Exception:  # noqa: BLE001
                pass

        self._sso_login(page)
        self._save_session_cache()
        return page

    def _probe_token_valid(self, page) -> bool:
        """轻量探测应用 token 是否真的可用。

        仅靠「sessionStorage 里有 token」或「页面渲染出来」都不可靠 —— 前者
        是我们自己注入的（过期也在），后者 Vue 会渲染壳子但接口静默失败。
        这里直接调一次轻量接口，看服务端认不认账。最多 3 秒。
        """
        import datetime as _dt
        date_str = (_dt.date.today() + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                ret = page.evaluate("""async (date) => {
                    const tok = sessionStorage.getItem('jsq_p-token') || '';
                    if (!tok) return {ok: false, reason: 'no-token'};
                    try {
                        const r = await fetch(
                            '/jsq/static/frontApi/res/roomListByCondition'
                            + '?date=' + date + '&building=' + encodeURIComponent('主校区'),
                            {headers: {'token': tok}});
                        const t = await r.text();
                        return {ok: !t.includes('token访问过期') && !t.includes('系统异常'),
                                body: t.slice(0, 200)};
                    } catch (e) { return {ok: false, reason: String(e)}; }
                }""", date_str)
            except Exception:  # noqa: BLE001
                ret = None
            if ret and ret.get("ok"):
                return True
            page.wait_for_timeout(300)
        return False

    def _sso_login(self, page):
        """完整 SSO 登录：用条件等待替代 networkidle 与固定 sleep。

        坑点：CAS cookie 还活着但应用 token 已过期时，回到首页能正常渲染壳子
        但所有业务接口 401 / token 访问过期。如果不在这里主动清 cookie、强制走
        SSO 表单，token 永远刷不出来，每次都"已登录态"短退出但预约必失败。
        """
        logger.info("打开 SSO 登录页")
        # domcontentloaded + 条件等隐藏字段，比 networkidle 快得多
        page.goto(SSO_URL, wait_until="domcontentloaded", timeout=30000)

        # 带有效 cookie 时会直接跳回 seat。但此时 token 可能已过期，要主动验证：
        # 走 2~3 秒轮询看是否能拿到房间数据，拿不到就清 cookie 强制走 SSO 表单。
        if "seat.ujn.edu.cn" in page.url and "sso.ujn.edu.cn" not in page.url:
            ok = self._probe_token_valid(page)
            if ok:
                logger.info("检测到已登录态（cookie + token 均有效）")
                return page
            logger.warning("cookie 有效但应用 token 已失效，清空 cookie 强制走 SSO 表单")
            try:
                self.context.clear_cookies()
            except Exception:  # noqa: BLE001
                pass
            page.goto(SSO_URL, wait_until="domcontentloaded", timeout=30000)

        # 等待 lt 等隐藏字段就绪（这些字段是 type="hidden"，不能用默认 visible 状态）
        page.wait_for_selector("input[name='lt']", state="attached", timeout=15000)

        # 调用页面自带的 strEnc 生成 rsa，填充隐藏字段后直接提交表单
        res = page.evaluate("""([u, p]) => {
            if (typeof strEnc !== 'function') return {error: 'strEnc not loaded'};
            const lt = document.querySelector('input[name="lt"]').value;
            const execution = document.querySelector('input[name="execution"]').value;
            const rsa = strEnc(u + p + lt, '1', '2', '3');
            document.getElementById('rsa').value = rsa;
            document.getElementById('ul').value = String(u.length);
            document.getElementById('pl').value = String(p.length);
            return {ok: true, rsa_len: rsa.length, lt_len: lt.length, execution: execution};
        }""", [self.cfg.username, self.cfg.password])
        logger.debug("SSO 加密结果: %s", res)
        if res.get("error"):
            raise RuntimeError(f"SSO 登录页缺少 strEnc: {res['error']}")

        self._save_debug("before_login")
        page.evaluate("""() => { document.getElementById('loginForm').submit(); }""")
        logger.debug("已提交 SSO 登录表单")

        # 条件等待 CAS 票据校验完成并跳回 seat（替代 80×500ms 盲等）
        try:
            page.wait_for_function(
                """() => location.href.indexOf('seat.ujn.edu.cn') >= 0
                      && location.href.indexOf('sso.ujn.edu.cn') < 0""",
                timeout=25000, polling=200)
        except Exception:  # noqa: BLE001
            logger.warning("登录后未如期跳回 seat 站点")

        # 条件等待 token 落位（替代固定 sleep 2 秒）
        try:
            page.wait_for_function(
                "(k) => !!sessionStorage.getItem(k)", TOKEN_KEY,
                timeout=15000, polling=200)
        except Exception:  # noqa: BLE001
            logger.debug("未等到 %s，继续", TOKEN_KEY)

        self._save_debug("after_login")
        logger.info("登录完成，当前 URL=%s", page.url)
        return page

    def _save_debug(self, name: str):
        if not self.cfg.get("debug"):
            return
        d = Path(__file__).resolve().parent.parent / "debug"
        d.mkdir(exist_ok=True)
        try:
            self.page.screenshot(path=str(d / f"{name}.png"))
            logger.debug("调试截图: %s", d / f"{name}.png")
        except Exception as exc:  # noqa: BLE001
            logger.debug("截图失败: %s", exc)

    @property
    def alive(self) -> bool:
        """会话是否还活着、page 处于 seat 域（且不在 SSO 登录页）。

        给 GUI 的预热缓存复用做健康检查：浏览器进程没被外部关闭、
        page 仍可达、URL 是 seat.ujn.edu.cn 业务域。"""
        try:
            if not self.page:
                return False
            url = self.page.url or ""
            if "seat.ujn.edu.cn" not in url:
                return False
            if "sso.ujn.edu.cn" in url:
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_home(self) -> bool:
        """确保 page 停在 #/main/home 且房间卡片已渲染。

        与 goto_home 的区别：**已经在首页且卡片已渲染时立即返回，不耗时**。
        定时抢座在「预热阶段」就调它把页面停到首页，准点提交时第二次调用
        会直接命中快路径 —— 不把这几秒的渲染等待留到开放之后。
        """
        try:
            url = self.page.url or ""
            if "seat.ujn.edu.cn" in url and "#/main/home" in url:
                n = self.page.evaluate(
                    """() => document.querySelectorAll('.room.el-col').length""")
                if n and n > 0:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return self.goto_home()

    def goto_home(self) -> bool:
        """在不重启浏览器的前提下，让 page 回到 #/main/home（条件等待就绪）。

        返回是否成功。供 GUI 在「预热 → do_book」之间复用同一浏览器时调用，
        避免重新走一次 _is_logged_in 的 12s 上限轮询。"""
        try:
            self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:  # noqa: BLE001
            return False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                n = self.page.evaluate(
                    """() => document.querySelectorAll('.room.el-col').length""")
                if n and n > 0:
                    return True
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(150)
        return False

    def close(self):
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def login_browser(cfg: Config, headless: Optional[bool] = None) -> BrowserSession:
    """便捷函数：登录并返回活跃会话。"""
    sess = BrowserSession(cfg, headless=headless)
    sess.open()
    return sess
