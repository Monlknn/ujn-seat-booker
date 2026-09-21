"""预约编排模块。

三种模式：
  - browser ：全程浏览器（登录 + 选座 + 拖滑块 + 提交）。
            济大预约流程走前端 TAC 滑块验证码，browser 是当前可用模式。
  - api     ：纯 REST 接口直连（旧 /rest/v2/ 已失效，不推荐）。
  - auto    ：优先 browser；api 仅作为历史兼容。

默认使用 browser 模式。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional

from .config import Config, resolve_target_date
from .logger import logger

# 浏览器模式下的选座/提交流程（因不同版本 DOM 差异较大，保留可扩展钩子）
from . import api as rest
from . import session as br
from . import slider as slv


def _wait_until(target_hhmm: str, label: str):
    """阻塞到当天 HH:MM（用于定时预约）。"""
    now = datetime.now()
    h, m = target_hhmm.split(":")
    target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if target <= now:
        return
    secs = (target - now).total_seconds()
    logger.info("%s 等待 %s 秒至 %s", label, int(secs), target_hhmm)
    time.sleep(secs)


def _book_via_api(cfg: Config) -> bool:
    token = rest.get_token(cfg.username, cfg.password)
    if not token:
        return False
    room_id = rest.get_room_id(token, cfg.get("room"))
    if not room_id:
        return False
    date = rest._resolve_date(cfg.get("date", "tomorrow"))
    seat_id = rest.get_seat_id(token, room_id, cfg.get("seat"), date)
    if not seat_id:
        return False
    _api_start, _api_end = cfg.time_window()
    resp = rest.free_book(token, seat_id, _api_start,
                           _api_end, cfg.get("date", "tomorrow"))
    if rest.is_captcha_blocked(resp):
        logger.warning("API 预约被滑块拦截，需切换浏览器模式")
        return False
    if resp.get("status") == "success":
        logger.info("API 预约成功: %s", resp.get("data"))
        return True
    logger.warning("API 预约失败: %s", resp.get("message", resp))
    # code=1 之类非滑块失败（如座位已被占）也判失败
    return False


def _make_date(cfg_date: str) -> str:
    """把配置里的 today/tomorrow/YYYY-MM-DD 转为 makeDate 字符串。

    实现统一委托给 config.resolve_target_date，保证「日期解析」只有一处逻辑
    （周二特例判定也用同一个函数，避免两处算法漂移）。
    """
    return resolve_target_date(cfg_date)


def _wait_for(page, expr: str, arg=None, timeout_ms: int = 8000,
              poll_ms: int = 250) -> bool:
    """浏览器端轮询等待 JS 条件成立（替代固定 sleep）。

    条件一旦满足立即返回，避免「页面早已就绪却还要 sleep」的时间浪费；
    超时未达成也返回 False，不影响既有降级逻辑（最多等待 timeout_ms）。
    expr 为 JS 表达式字符串，或 ``(arg)=>{...}`` 函数字符串（配合 arg 透传）。
    """
    try:
        if arg is None:
            page.wait_for_function(expr, timeout=timeout_ms, polling=poll_ms)
        else:
            page.wait_for_function(expr, arg, timeout=timeout_ms, polling=poll_ms)
        return True
    except Exception:
        return False


def _close_drawer(page):
    """按 Esc 关闭可能的遮罩/抽屉；若无遮罩则立即返回（不空等）。"""
    try:
        page.keyboard.press("Escape")
        # 轮询等待可见遮罩消失（无遮罩时第一次轮询即满足条件，几乎零等待）
        _wait_for(page, """() => {
            const ov = [...document.querySelectorAll('.el-overlay,.v-modal')];
            const visible = ov.filter(o => {
                const s = getComputedStyle(o);
                const r = o.getBoundingClientRect();
                return s.display !== 'none' && r.width > 0 && r.height > 0;
            });
            return visible.length === 0;
        }""", timeout_ms=800, poll_ms=80)
    except Exception:
        pass


def _goto_home(page):
    """回到首页并关闭可能的公告抽屉（条件等待，不空等 3 秒）。"""
    page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
              wait_until="domcontentloaded", timeout=20000)
    # 条件等待首页控件挂载（Vue 就绪），挂载完立即继续
    _wait_for(page, """() => {
        const inp = [...document.querySelectorAll('.el-select input.el-input__inner')];
        return inp.some(i => (i.placeholder||'').trim()==='场馆选择')
            || inp.some(i => (i.placeholder||'').trim()==='楼层');
    }""", timeout_ms=5000, poll_ms=200)
    _close_drawer(page)


def _select_el_dropdown(page, placeholder: str, option_text: str,
                        timeout_ms: int = 5000) -> bool:
    """按 placeholder 定位 el-select 触发下拉并选 option_text。
    el-select 选项为异步加载（尤其楼层/校区），这里「轮询等待选项出现 → 点击 →
    回读校验」，避免固定 500ms 等待导致选项未就绪而点不到（卡在楼层）。
    返回是否成功。空字符串 / '全部' 视为不选。"""
    if not option_text or option_text == "全部":
        return False
    try:
        opened = page.evaluate("""(ph) => {
            const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
            const inp = inputs.find(i => (i.placeholder||'').trim() === ph);
            if (inp) { inp.click(); return true; }
            return false;
        }""", placeholder)
        if not opened:
            logger.warning("未找到下拉输入框 placeholder=%s", placeholder)
            return False

        deadline = int(time.time() * 1000) + timeout_ms
        while int(time.time() * 1000) < deadline:
            res = page.evaluate("""(args) => {
                const ph = args.ph, target = args.target;
                const norm = (s) => (s||'').trim();
                const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
                const inp = inputs.find(i => norm(i.placeholder) === ph);
                const drops = [...document.querySelectorAll('.el-select-dropdown')];
                const visible = drops.filter(d => {
                    const r = d.getBoundingClientRect();
                    return r.width>0 && r.height>0 && getComputedStyle(d).display!=='none';
                });
                const dd = visible[0] || drops[0];
                if (!dd) return 'no-dropdown';
                const items = [...dd.querySelectorAll('.el-select-dropdown__item')];
                let it = items.find(e => norm(e.textContent) === target);
                if (!it) it = items.find(e => norm(e.textContent).includes(target));
                if (it) { it.click(); return 'clicked'; }
                return 'waiting:' + items.length;
            }""", {"ph": placeholder, "target": option_text})
            if res == "clicked":
                # 回读校验：输入框的值是否已变成目标
                val = page.evaluate("""(args) => {
                    const ph = args.ph;
                    const norm = (s) => (s||'').trim();
                    const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
                    const inp = inputs.find(i => norm(i.placeholder) === ph);
                    return inp ? (inp.value||'') : '';
                }""", {"ph": placeholder})
                if option_text in val or val in option_text or val == option_text:
                    page.keyboard.press("Escape")
                    # 条件等待下拉收起（无需固定 300ms 兜底休眠）
                    _wait_for(page, """() => {
                        const d = document.querySelector('.el-select-dropdown');
                        if (!d) return true;
                        return getComputedStyle(d).display==='none'
                            || d.getBoundingClientRect().width===0;
                    }""", timeout_ms=500, poll_ms=80)
                    return True
                # 值未对上，可能点到但渲染慢，再等一轮
            page.wait_for_timeout(400)
        logger.warning("下拉 %s 选 %s 超时（异步选项未就绪）", placeholder, option_text)
        page.keyboard.press("Escape")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("下拉 %s 选 %s 失败: %s", placeholder, option_text, exc)
        return False


def _apply_home_filter(page, campus: str = "", floor: str = "") -> None:
    """在 #/main/home 上应用「校区（场馆选择）」与「楼层」筛选。
    campus floor 留空 / '全部' / '主校区'(默认值) 不会触发切换。

    ★ 2026-09-04 修正：SPA 在「点击当前已选项」时不会重新触发 findRoomDurationFun。
       为强制触发请求，先点「全部」再点目标 floor（同一值 click 不会触发 Vue watch）。
    """
    if campus and campus not in ("主校区", "全部", ""):
        if _select_el_dropdown(page, "场馆选择", campus):
            logger.info("已切校区: %s", campus)
            # 条件等待校区筛选生效（房间列表重新渲染）
            _wait_for(page, """() => document.querySelectorAll('.room.el-col').length > 0""",
                      timeout_ms=1000, poll_ms=200)
            _close_drawer(page)
    if floor and floor not in ("全部", ""):
        # ★ 先点「全部」让 SPA 真正触发 findRoomDurationFun，再点目标 floor
        if _select_el_dropdown(page, "楼层", "全部"):
            _wait_for(page, """() => document.querySelectorAll('.room.el-col').length > 0""",
                      timeout_ms=1000, poll_ms=200)
            _close_drawer(page)
            page.wait_for_timeout(400)
        if _select_el_dropdown(page, "楼层", floor):
            logger.info("已切楼层: %s", floor)
            # 条件等待楼层筛选生效（房间列表重新渲染）
            _wait_for(page, """() => document.querySelectorAll('.room.el-col').length > 0""",
                      timeout_ms=1000, poll_ms=200)
            _close_drawer(page)


def _reservation_exists(page, make_date: str, room: str,
                        start_mm: str, end_mm: str,
                        retries: int = 1, retry_delay_ms: int = 1200) -> bool:
    """通过"记录查询 → 今日预约"卡片核对目标时段是否已有预约。

    前端 frontApi 带 HMAC 签名，无法用 page.request 直接调用；但页面自身
    会带签名拉取"我的预约"并渲染成卡片。这里只读渲染后的卡片文本，
    判断 make_date + room + 起止时间 是否同时出现，作为下单是否成功的铁证。

    退出前保证页面回到首页：无论命中与否、中途是否异常，都通过 goto_home
    兜底，避免后续选座流程在 `#/main/my` 等其它页面里误点把页面拉走。

    【2026-09-05 增强】加重试：刚成功下单后，后端通常要 1~3 秒才会渲染到「记录
    查询」卡片，第一次没匹配会自动重试（最多 retries 次，间隔 retry_delay_ms 毫秒）。
    单次调用仍工作（retries=1），向后兼容。
    """
    if retries < 1:
        retries = 1
    last_match = False
    try:
        for attempt in range(retries):
            if attempt > 0:
                # 重试间隔前必须先回首页（否则可能还停在 `#/main/my`，
                # 卡片因为没有重新触发 list 不会刷新）
                _goto_home(page)
                page.wait_for_timeout(retry_delay_ms)
            # 打开"记录查询"（首页默认不显示未来预约，需进记录页的"今日预约"标签）
            try:
                page.get_by_text("记录查询", exact=False).first.click(timeout=4000)
                # 条件等待记录页加载（出现"今日预约/暂无预约"或目标房间/日期文本）
                _wait_for(page, """(args) => {
                    const want = [args.d, args.r];
                    const leaves = [];
                    document.querySelectorAll('*').forEach(e => {
                        if (e.children.length === 0) {
                            const t = (e.textContent||'').trim();
                            if (t) leaves.push(t);
                        }
                    });
                    const txt = leaves.join(' ');
                    if (leaves.length > 30) return true;
                    if (/今日预约|暂无预约|预约记录|我的预约/.test(txt)) return true;
                    return want.some(w => txt.includes(w));
                }""", {"d": make_date, "r": room}, timeout_ms=3500, poll_ms=200)
                _close_drawer(page)
            except Exception:
                # 找不到"记录查询"或点击失败 → 当作"未命中"，让首页兜底校验
                pass
            cards = page.evaluate("""() => {
                const txt = [];
                document.querySelectorAll('*').forEach(e => {
                    if (e.children.length === 0) {
                        const t = (e.textContent || '').trim();
                        if (t) txt.push(t);
                    }
                });
                return txt;
            }""")
            # 卡片文本分散在多个叶子节点，需在"全部文本"中同时出现四个要素
            all_text = " ".join(cards)
            last_match = bool(
                make_date in all_text and room in all_text
                and start_mm in all_text and end_mm in all_text
            )
            if last_match:
                break
        return last_match
    finally:
        # 无论命中 / 未命中 / 异常，都强制回到首页，规避后续点错元素
        _goto_home(page)


def _js_root_vm():
    return """function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }"""


def _js_find_vm():
    return """function findVM(vm,pred){ if(!vm)return null; if(pred(vm))return vm; for(const c of (vm.$children||[])){const r=findVM(c,pred); if(r)return r;} return null; }"""


def _setup_dry_run_route(page):
    """dry-run 模式下拦截浏览器 freeBook 请求，避免生成真实预约记录。"""
    def handler(route):
        logger.warning("[dry-run] 拦截 freeBook: %s", route.request.url)
        route.abort()
    page.route("**/freeBook**", handler)


def _evidence_verified(page, cfg: Config, evidence: str) -> bool:
    """所有「软判据」通过后的最终铁证：用「记录查询」卡片核对目标时段是否真在后台。

    返回 True：铁证成功（前台 + 后台双向确认，最稳）。
    返回 False：仅前端判成功（前台软判据通过，但后台卡片暂未查到 —— 仍视为
                整个流程成功，因为可能是网络延迟渲染；只是日志 WARN 提醒）。

    dry-run 模式不验证（本来就是虚拟，没真记录可查）。

    evidence: 用于日志的简短来源描述（如 "vm.orderSuccess" / "orderObj=RESERVE" / "ctId 已有"）。
    """
    if cfg.get("dry_run"):
        return True
    md = _make_date(cfg.get("date", "tomorrow"))
    room = cfg.get("room", "第一阅览室")
    # 必须用「实际生效的时间窗」核对：周二会换成 08:30~12:00，
    # 若此处仍用 config 里的 08:20~22:00 去比对，铁证必然查不到。
    sm, em = cfg.time_window()
    # 最多 7 秒：5 次重试 × 1.2 秒间隔。足够覆盖后端异步渲染延迟。
    hit = _reservation_exists(page, md, room, sm, em,
                              retries=5, retry_delay_ms=1200)
    if hit:
        logger.info("✓ 铁证成功（前后台双向确认）[%s]：%s %s %s~%s",
                    evidence, room, md, sm, em)
        return True
    logger.warning("⚠️ 仅前端判成功（%s），后台预约记录暂未查到 —— "
                   "建议人工核对「我的预约」或重跑流程", evidence)
    return False


async def _modify_get_start_times(route, user_start_str: str):
    """拦截 getStartTimes 接口响应，把 startTimes[0].value 改为用户值。

    原因：layout 路由组件的 getStartTimeByMinTime() 在 dateType != '1' 时会拉
    getStartTimes 接口，异步响应里把 startTimes[0] 的 value 写到
    vm.search.starTimeValue（默认是 420 = 07:00）。我们直接改响应体里第一项
    的 value，layout 写的就是用户想要的开始时间。

    响应结构是 dict，data 是 [[value, text], [value, text], ...]
    """
    try:
        response = await route.fetch()
        try:
            body = await response.json()
        except Exception:
            await route.fulfill(response=response)
            return
        if isinstance(body, dict) and isinstance(body.get("data"), list) and body["data"]:
            base = int(user_start_str)
            # 把第一项改成用户值，让 layout 默认选中
            body["data"][0] = [user_start_str, _fmt_mm(base)]
            # 后续项按 30 分钟步长递增（如果原列表更长）
            for i in range(1, len(body["data"])):
                v = base + i * 30
                body["data"][i] = [str(v), _fmt_mm(v)]
            body["status"] = True
            await route.fulfill(json=body)
        else:
            await route.fulfill(response=response)
    except Exception as exc:  # noqa: BLE001
        # 拦截器自身失败时不要阻塞原请求
        try:
            await route.continue_()
        except Exception:
            pass
        if user_start_str:  # 调试
            import traceback
            print(f"[modify-start-times] error: {exc}\n{traceback.format_exc()[:300]}")


def _fmt_mm(minute: int) -> str:
    """分钟数 → HH:MM 显示文本。"""
    h, m = divmod(int(minute), 60)
    return f"{h:02d}:{m:02d}"


def _book_via_browser_on_page(page, cfg: Config, *,
                        owns_session: bool = False, sess=None) -> bool:
    """在已登录的 page 上完成预约全流程。

    拆分出来是为了让 GUI 可以「预热登录后保留浏览器，按下开始预约按钮
    时直接复用同一个 page」，省去 pw.launch + new_context + 12s 轮询。
    owns_session=True 时由本函数负责关闭 sess（用于 book_once 直接入口）。
    owns_session=False 时由调用方负责关闭（GUI 自己持有 sess）。
    """
    # ★ 预约时间窗在这里一次性解析，后续所有环节（路由拦截器 / 幂等预检 /
    #   弹窗设值 / 铁证核对）统一复用，避免各写各的导致「提交 08:20 却核对 08:30」。
    #   周二时 config.time_window() 会自动换成 08:30~12:00（图书馆 12:00~16:00 闭馆）。
    _tw_start, _tw_end = cfg.time_window()
    _tw_note = cfg.time_window_note()
    if _tw_note:
        logger.info("时间窗调整：%s", _tw_note)
    try:
        if cfg.get("dry_run"):
            _setup_dry_run_route(page)
            logger.warning("[dry-run] 已启用 freeBook 拦截，本次不会提交真实预约")
        logger.info("浏览器模式：已登录，URL=%s", page.url)

        # 调试：捕获 freeBook 响应，便于排查真实预约失败原因。
        # 始终填充 —— 「弹窗已关闭但未成功」分支需要它判断成功（vm.orderSuccess 可能漏读）。
        # 关键：dry-run 模式下 freeBook 被 route.abort() 拦截，**不会**触发 response 事件，
        # 但 request / requestfailed 事件仍会触发。所以同时挂多个监听器，把 dry-run
        # 下拦截的请求也记录下来（作为「滑块通过 + freeBook 确实发出去了」的证据）。
        _captured_freebook = []
        _captured_requests = []

        def _on_resp(response):
            if "freeBook" in response.url:
                # ★ 诊断：先记录所有响应事件，包括失败的解析
                entry = {"status": response.status, "body": None, "raw": None,
                         "url": response.url, "ok": response.ok}
                try:
                    entry["body"] = response.json()
                except Exception as exc:  # noqa: BLE001
                    entry["json_err"] = str(exc)
                    try:
                        entry["body"] = response.text()
                    except Exception as exc2:  # noqa: BLE001
                        entry["text_err"] = str(exc2)
                if cfg.get("debug"):
                    logger.debug("[fb-resp] status=%s ok=%s body=%s",
                                 response.status, response.ok,
                                 {k: v for k, v in entry.items() if k in ("body", "json_err", "text_err")})
                _captured_freebook.append(entry)

        def _on_req(request):
            if "freeBook" in request.url:
                _captured_requests.append({"url": request.url, "method": request.method})

        def _on_req_failed(request):
            if "freeBook" in request.url:
                if cfg.get("debug"):
                    logger.debug("[fb-reqfail] %s %s failure=%s",
                                 request.method, request.url, request.failure)
                _captured_freebook.append({
                    "status": "aborted", "url": request.url, "method": request.method,
                    "failure": str(request.failure) if request.failure else ""})

        page.on("response", _on_resp)
        page.on("request", _on_req)
        page.on("requestfailed", _on_req_failed)
        if cfg.get("debug"):
            logger.debug("已订阅 freeBook 响应/请求/失败事件（用于结果判定）")

        # ★★★ 关键：拦截 getStartTimes 接口必须在点击阅览室之前注册。
        #    layout 路由组件 mounted → getStartTimeByMinTime → 立即发 getStartTimes，
        #    响应回来后用 startTimes[0]（默认 420=07:00）覆盖 starTimeValue。
        #    弹窗 step 7 才注册太晚，必须在这里提前注册。
        start_mm = _tw_start
        end_mm = _tw_end
        _h, _m = start_mm.split(":")
        _pre_route_user_start = str(int(_h) * 60 + int(_m))
        _pre_route_user_end = _pre_route_user_start  # 仅用于注册标记，不参与响应
        page.route("**/static/frontApi/res/getStartTimes/**",
                   lambda route: _modify_get_start_times(route, _pre_route_user_start))
        if cfg.get("debug"):
            logger.debug("已注册 getStartTimes 拦截器（userStart=%s）", _pre_route_user_start)

        # 1) 进入首页并关闭"系统公告"遮罩
        _goto_home(page)

        # 1.4) 应用校区 + 楼层筛选：选了"7层"后，"第七阅览室*"会和其他
        # 第七楼阅览室一起出现在单一未分页视图，避免只能看到第 1 页的默认集合。
        _apply_home_filter(page, campus=cfg.get("campus", ""),
                           floor=cfg.get("floor", ""))

        # 1.5) 幂等预检：目标时段若已有有效预约，直接视为成功，避免重复下单/冲突报错
        if not cfg.get("dry_run"):
            _room = cfg.get("room", "第一阅览室")
            _md = _make_date(cfg.get("date", "tomorrow"))
            _sm = _tw_start
            _em = _tw_end
            if _reservation_exists(page, _md, _room, _sm, _em):
                logger.info("目标时段已有有效预约（幂等跳过，不重复下单）：%s %s %s~%s",
                            _room, _md, _sm, _em)
                return True

        # 2) 点击目标阅览室 —— 先确保在首页（_reservation_exists 已在 finally
        #    回到首页，但兜底再来一次），再用「唯一可见卡片」点击，
        #    避免 get_by_text(...partial) 在 #/main/my 上误匹配到卡片文字。
        room_name = cfg.get("room", "第一阅览室")
        logger.info("选择阅览室: %s", room_name)
        clicked_ok = False
        for attempt in range(2):
            try:
                # 在首页卡片上精确点击：限定为首页容器内的「阅览室」文本
                ok = page.evaluate("""(name) => {
                    function escapeText(s){ return (s||'').replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'); }
                    function isRoomCardTitle(el){
                        const t=(el.textContent||'').trim();
                        if(!/阅览室/.test(t) || t.length>24) return false;
                        // 没有子节点的纯文字节点（即标题文字）
                        return el.children.length===0;
                    }
                    let node=null;
                    // 1) 优先：精确等于房间名
                    [...document.querySelectorAll('*')].forEach(e=>{
                        if(isRoomCardTitle(e) && (e.textContent||'').trim()===name) {
                            // 不要已点过的；同一个文字节点只取一次
                            if(!node) node=e;
                        }
                    });
                    // 2) 找不到精确 → 模糊但要求「以 name 开头或精确包含」
                    if(!node){
                        [...document.querySelectorAll('*')].forEach(e=>{
                            if(!isRoomCardTitle(e)) return;
                            const t=(e.textContent||'').trim();
                            if(t===name || t.startsWith(name+' ') || t.endsWith(' '+name)) {
                                if(!node) node=e;
                            }
                        });
                    }
                    if(!node) return false;
                    // 沿父链找最近的「可点击卡片」触发 click
                    let cur=node;
                    for(let i=0;i<6 && cur && cur!==document.body;i++){
                        const r=cur.getBoundingClientRect();
                        if(r.width>40 && r.height>20){
                            cur.click();
                            return true;
                        }
                        cur=cur.parentElement;
                    }
                    node.click();
                    return true;
                }""", room_name)
                if not ok:
                    raise RuntimeError(f"未在首页 DOM 中找到卡片 {room_name}")
                clicked_ok = True
                break
            except Exception as exc:
                logger.warning("阅览室点击失败(尝试 %d): %s", attempt + 1, exc)
                # 兜底：显式回首页 + 关抽屉 + 等一秒再重试
                _goto_home(page)
                _close_drawer(page)
                # 重新应用楼层/校区（_goto_home 会清空）
                _apply_home_filter(page, campus=cfg.get("campus", ""),
                                   floor=cfg.get("floor", ""))
                _wait_for(page, """() => document.querySelectorAll('.room.el-col').length > 0""",
                          timeout_ms=1200, poll_ms=200)
        if not clicked_ok:
            logger.error("阅览室 %s 多次点击仍失败，放弃本次预约", room_name)
            return False
        # 不再固定等待 5 秒：立即进入 layout 就绪轮询（见下方 step 4）
        _close_drawer(page)

        make_date = _make_date(cfg.get("date", "tomorrow"))
        logger.info("预约日期: %s", make_date)

        # 3) 强制把 URL 上的 makeDate 修正到目标日期。
        #    重要：这里不能依赖 route.roomId 是否为 None —— 选阅览室后第一次
        #    evaluate 时 root vm 还没绑到 $route 上，$route.query.id 会取空，
        #    导致「if roomId」的旧条件永远不成立、URL 也不会跳转，最终 freeBook
        #    仍用浏览器地址栏/卡片数据里写的「今天」的 makeDate 去占今天的座位。
        #    改成无条件从 location.hash 兜底解出 id，再判断 urlDate 是否匹配目标。
        route = page.evaluate(f"""() => {{
            {_js_root_vm()}
            {_js_find_vm()}
            const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
            const q = lp && lp.$route && lp.$route.query || {{}};
            return {{roomId: q.id, urlDate: q.makeDate, url: location.href,
                    hash: location.hash || ''}};
        }}""")
        logger.debug("当前 route: %s", route)
        rid = route.get("roomId")
        if not rid:
            try:
                rid = page.evaluate(
                    """() => {
                        const m = (location.hash||'').match(/[?&]id=([^&]+)/);
                        return m ? decodeURIComponent(m[1]) : '';
                    }""")
            except Exception:
                rid = ''
        url_date = route.get("urlDate") or ""
        if rid and url_date != make_date:
            url = (f"https://seat.ujn.edu.cn/jsq-v/#/main/layout"
                   f"?id={rid}&makeDate={make_date}")
            logger.info("修正日期(URL上=%s → 目标=%s): %s", url_date, make_date, url)
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # 不再固定等待 4.5 秒：交给下方 layout 就绪轮询
            _close_drawer(page)

        # 4) 等待 canvas / layout VM 就绪（细粒度轮询，页面一就绪立即继续）
        #    ★ 2026-09-17 实测：登录后首次导航到 layout 时，SPA 冷启动可能 >6 秒
        #    （原 20×300ms=6s 曾现场超时并报「座位布局页面未加载完成」）。
        #    放宽到 40×300ms=12s，与下方座位图轮询口径一致，降低 07:00 丢单风险。
        layout_pred = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"
        layout_ok = False
        for attempt in range(40):
            ok = page.evaluate(f"""() => {{
                {_js_root_vm()}
                {_js_find_vm()}
                return !!findVM(rootVM(), {layout_pred});
            }}""")
            if ok:
                layout_ok = True
                if attempt > 0:
                    logger.info("座位布局就绪（第 %d 次轮询）", attempt + 1)
                break
            page.wait_for_timeout(300)
        if not layout_ok:
            logger.error("座位布局页面未加载完成（已等待约 12 秒）")
            return False

        # 4.5) 等待「座位图」真正绘制完成 —— layout VM 就绪 ≠ 座位图加载完成。
        # 济大每天 07:00 开放次日预约时，准点跳进 layout 经常出现 getObjects() 短暂返回空
        # （座位图异步加载，roomSeatMap 接口响应可能比 layout 慢 0.5~3 秒）。
        # 这里轮询座位数 > 0，最多 ~10 秒（40 次 × 250ms）。之前 7:00 准点抢座
        # 因没等座位图就直接 getObjects → 0 个座位被判 "no seat" 失败。
        seat_loaded = False
        for attempt in range(40):
            seat_count = page.evaluate(f"""() => {{
                {_js_root_vm()}
                {_js_find_vm()}
                const vm = findVM(rootVM(), {layout_pred});
                if (!vm) return -1;
                try {{
                    const objs = vm.seatPreview.design.getObjects();
                    return objs.filter(o => o.seat).length;
                }} catch (e) {{ return -2; }}
            }}""")
            if isinstance(seat_count, int) and seat_count > 0:
                if attempt > 0:
                    logger.info("座位图加载完成（第 %d 次轮询，%d 个座位）", attempt + 1, seat_count)
                seat_loaded = True
                break
            page.wait_for_timeout(250)
        if not seat_loaded:
            logger.error("座位图未加载完成（layout VM 就绪但座位数=0，济大可能未到开放时间或接口临时不可用）")
            return False

        # 5) 选座并点击
        target_seat = str(cfg.get("seat", "001"))
        pick = page.evaluate(f"""(target) => {{
            {_js_root_vm()}
            {_js_find_vm()}
            const vm = findVM(rootVM(), {layout_pred});
            const objs = vm.seatPreview.design.getObjects().filter(o => o.seat);
            let targetObj=null, firstFree=null;
            for (const o of objs){{
                const s = o.seat;
                const num = s.label!==undefined ? s.label : (s.id!==undefined ? s.id : (s.name!==undefined?s.name:null));
                if (s.status==='FREE' && !firstFree) firstFree={{obj:o, num:String(num), id:s.id}};
                if (target && (String(s.label)===String(target) || String(s.id)===String(target) ||
                               (s.name && s.name.includes(String(target))))) targetObj={{obj:o, num:String(num), id:s.id}};
            }}
            if (!targetObj && firstFree) targetObj=firstFree;
            if (!targetObj) return {{err:'no seat', count:objs.length}};
            const o = targetObj.obj;
            const c = o.getCenterPoint();
            const vpt = vm.seatPreview.design.viewportTransform;
            const zoom = vpt[0];
            const ix = c.x*zoom + vpt[4];
            const iy = c.y*zoom + vpt[5];
            const cvs = document.querySelector('#seatLayoutPreviewCanvas');
            const r = cvs.getBoundingClientRect();
            const scale = r.width / vm.seatPreview.design.getWidth();
            const sx = r.left + ix*scale;
            const sy = r.top + iy*scale;
            return {{ok:true, chosen:targetObj.num, seatId:String(targetObj.id),
                    px:{{x:Math.round(sx), y:Math.round(sy)}},
                    info:{{zoom, vpt, canvasW:vm.seatPreview.design.getWidth(), rectW:Math.round(r.width)}}}};
        }}""", target_seat)
        if pick.get("err"):
            logger.error("未找到可预约座位: %s", pick)
            return False
        logger.info("选中座位 %s (id=%s)，点击 (%s,%s)",
                    pick["chosen"], pick["seatId"], pick["px"]["x"], pick["px"]["y"])
        page.mouse.click(pick["px"]["x"], pick["px"]["y"])
        # 不再固定等待 2.5 秒：交给下方"预约弹窗"轮询（step 6）

        JS_DIALOG_ACTION = """
            function rootVM(){ const app=document.getElementById('app'); if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
            function walkAll(vm,acc){ if(!vm)return; if(typeof vm.codeCheck==='function'&&typeof vm.confirmFilter==='function') acc.push(vm); for(const c of (vm.$children||[])) walkAll(c,acc); }
            const root=rootVM(); const arr=[]; walkAll(root,arr);
            const vm = arr.find(v=>v.show===true||v.showSelectModal===true||v.showCodeCheck===true) || arr[0] || null;
        """

        # 6) 等待预约弹窗打开
        dialog_open = False
        for _ in range(8):
            dialog_open = page.evaluate(f"""() => {{{JS_DIALOG_ACTION} return !!vm; }}""")
            if dialog_open:
                break
            page.wait_for_timeout(500)
        if not dialog_open:
            logger.error("预约弹窗未打开")
            return False
        logger.info("预约弹窗已打开")

        # 7) 设置开始/结束时间
        #    ★★★ 关键 bug：弹窗打开后，Vue 端异步调用 getStartTimeByMinTime() 拉
        #    startTimes 列表，并在响应回来时用【列表第一个值（=默认 420=07:00）覆盖】
        #    starTimeValue。如果在异步响应回来之前就 set，会被它覆盖。所以必须：
        #      ① hook getStartTimeByMinTime：异步响应回来时强制覆盖用户值
        #      ② 设置 starTimeValue → 触发 watch → getEndTimeByStartime（异步）
        #      ③ 等 endTimes 列表更新（基于新 startTime）
        #      ④ 再设 endTimeValue
        #    反过来先设 endTime 再设 startTime 也行不通，因为 watch 触发
        #    getEndTimeByStartime 会重算 endTimes，可能让原 endTime 失效。
        start_mm = _tw_start
        end_mm = _tw_end

        def hhmm_to_min(hhmm: str) -> int:
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)

        start_min = hhmm_to_min(start_mm)
        end_min = hhmm_to_min(end_mm)
        logger.info("设置开始时间 starTimeValue=%s (~%s 分钟)，结束时间 endTimeValue=%s (~%s 分钟)",
                    start_mm, start_min, end_mm, end_min)

        # ① getStartTimes 拦截器已在函数开头注册（必须在 layout 路由 mounted 之前）
        logger.debug("getStartTimes 拦截器已在函数开头生效（userStart=%s）", start_min)

        def _poll_vm(expr: str, timeout_ms: int = 5000, poll_ms: int = 200):
            """轮询一个 JS 表达式直到它返回 truthy 或超时。返回最后一次的值。"""
            elapsed = 0
            last = None
            while elapsed < timeout_ms:
                last = page.evaluate(f"() => {{{JS_DIALOG_ACTION} return ({expr}); }}")
                if last:
                    return last
                page.wait_for_timeout(poll_ms)
                elapsed += poll_ms
            return last

        # ② 探测弹窗 mode/markMode：mode=0 才会走 getStartTimeByMinTime（异步覆盖 starTimeValue），
        #    mode != 0 走 getTimeSlice（不覆盖）。济大阅览室多半 markMode 字段不存在或为 null，
        #    此时 vm.mode 也是 undefined/null —— 也按 非0 处理（不阻塞等待 startTimes 列表）。
        mode_info = page.evaluate(f"""() => {{{JS_DIALOG_ACTION}
            const seatKeys = vm.seat ? Object.keys(vm.seat) : [];
            return {{
                mode: vm.mode,
                markMode: (vm.seat && vm.seat.markMode) != null ? vm.seat.markMode : null,
                seatId: vm.seat && vm.seat.id,
                show: vm.show,
                starTimeValue: vm.search.starTimeValue,
                endTimeValue: vm.search.endTimeValue,
                seatKeys: seatKeys,
            }};
        }}""")
        logger.info("[mode] %s", mode_info)
        # ★ 关键判定：markMode 严格等于 0 才走 startTimes 等待路径。
        #    之前用 (markMode == 0) 把 None/字符串 '0' 也会判为 true，是 bug。
        use_min_time_mode = mode_info.get("markMode") == 0

        # ③ 等 startTimes 列表非空（异步响应已完成，Vue 已写入默认值）
        #    只有 markMode=0 的阅览室才需要等；markMode != 0 跳过空等。
        if use_min_time_mode:
            st_ready = _poll_vm("(vm.search.startTimes||[]).length > 0", timeout_ms=5000)
            if not st_ready:
                logger.warning("startTimes 列表 5 秒内未就绪（markMode=0 但响应慢）")
        else:
            logger.debug("markMode != 0，跳过 startTimes 列表等待（节省 5 秒）")

        # ④ 显式设 starTimeValue（hook 已确保异步覆盖也能守住）
        ok = page.evaluate(f"""(args) => {{{JS_DIALOG_ACTION}
            if (!vm) return false;
            vm.search.starTimeValue = String(args.start);
            if (typeof vm.getEndTimeByStartime === 'function') {{
                try {{ vm.getEndTimeByStartime(); }} catch(e){{}}
            }}
            return {{
                start: vm.search.starTimeValue,
                endTimesLen: (vm.search.endTimes||[]).length,
            }};
        }}""", {"start": start_min})
        logger.debug("[step1] 设置 starTimeValue=%s 后回读: %s", start_min, ok)

        # ⑤ 等 endTimes 列表更新（基于新 startTime）。markMode != 0 时 endTimes 始终为空，跳过等待
        #    （Vue confirmFilter 只读 endTimeValue 的值，不依赖 endTimes 列表）
        if use_min_time_mode:
            end_ready = _poll_vm("(vm.search.endTimes||[]).length > 0", timeout_ms=5000)
            if not end_ready:
                logger.warning("endTimes 列表 5 秒内未就绪")
        else:
            logger.debug("markMode != 0，跳过 endTimes 列表等待")

        # ⑥ 再设 endTimeValue
        ok2 = page.evaluate(f"""(args) => {{{JS_DIALOG_ACTION}
            if (!vm) return false;
            vm.search.endTimeValue = String(args.end);
            return {{
                start: vm.search.starTimeValue,
                end: vm.search.endTimeValue,
                startTimesLen: (vm.search.startTimes||[]).length,
                endTimesLen: (vm.search.endTimes||[]).length,
            }};
        }}""", {"end": end_min})
        logger.debug("[step2] 设置 endTimeValue=%s 后回读: %s", end_min, ok2)
        if not ok2:
            logger.error("无法设置预约时间")
            return False

        # 8) 判断是否需要滑块
        mack = page.evaluate("""() => {
            try {
                const si = (typeof sessionStorageProxy!=='undefined' && sessionStorageProxy) ? sessionStorageProxy.getItem('systemInfo') : sessionStorage.getItem('systemInfo');
                if (!si) return null;
                return JSON.parse(si).mackCaptcha;
            } catch(e){ return null; }
        }""")
        logger.info("mackCaptcha=%s", mack)

        if mack:
            # 触发滑块渲染（★ debug：先读一下当前 search 字段，并 hook confirmFilter 看调用瞬间的值）
            pre = page.evaluate(f"""() => {{{JS_DIALOG_ACTION}
                // hook confirmFilter 记录调用前的 search 状态
                if (!window.__cfHooked) {{
                    const orig = vm.confirmFilter;
                    vm.confirmFilter = function() {{
                        window.__cfArgs = {{
                            star: vm.search.starTimeValue,
                            end: vm.search.endTimeValue,
                            min: vm.search.minMinutes,
                            show: vm.show,
                            arg0: arguments[0],
                        }};
                        window.__cfCalled = true;
                        return orig.apply(this, arguments);
                    }};
                    window.__cfHooked = true;
                }}
                return {{
                    starTimeValue: vm.search.starTimeValue,
                    endTimeValue: vm.search.endTimeValue,
                    minMinutes: vm.search.minMinutes,
                    show: vm.show,
                }};
            }}""")
            logger.info("[debug] 触发 codeCheck 前 search: %s", pre)
            page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) vm.codeCheck(); return !!vm; }}""")
            # 条件等待滑块 DOM 出现（渲染好立即处理，不空等 3 秒）
            _wait_for(page, """() => {
                const w = document.querySelector('#show-code-check-wrap');
                if (w && getComputedStyle(w).display!=='none' && w.getBoundingClientRect().width>0) return true;
                const bg = document.querySelector('#tianai-captcha-slider-bg-img');
                return !!(bg && bg.getBoundingClientRect().width>0);
            }""", timeout_ms=4000, poll_ms=200)
            # 解滑块
            solved = slv.solve(page, cfg)
            if not solved:
                logger.error("未处理到滑块，预约失败")
                return False
            logger.info("滑块已处理，等待预约结果...")
        else:
            # 无需滑块，codeCheck 会直接进入 confirmFilter -> freeBook
            logger.info("无需滑块，直接触发预约提交")
            page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) vm.codeCheck(); return !!vm; }}""")

        # 9) 等待结果
        # 滑块错误自动重试：
        #   - 服务器会自动给新滑块（拖错一次就刷）→ 脚本看到 bg src 变化就重新
        #     analyze+拖动，不主动刷新（避免和服务器行为叠加造成频繁换验证码）；
        #   - 服务器 5 轮（~2.5 秒）都没动 → 脚本主动 _reload_tac 强制刷新，
        #     最多 2 次（防止无限循环）。
        slider_error_streak = 0
        slider_retried = 0
        result: dict = {}
        cf_logged = False
        for _ in range(40):
            result = page.evaluate(f"""() => {{
                {JS_DIALOG_ACTION}
                if (!vm) return {{found:false, msg:'no dialog'}};
                const orderSuccess = !!vm.orderSuccess;
                const msg = (vm.orderObj && vm.orderObj.message) ? vm.orderObj.message : '';
                return {{
                    found:true, orderSuccess, msg,
                    show: vm.show, showSelectModal: vm.showSelectModal, showCodeCheck: vm.showCodeCheck,
                    cfCalled: !!window.__cfCalled,
                    cfArgs: window.__cfArgs || null,
                }};
            }}""")
            # 第一次看到 confirmFilter 被调用就记日志
            if not cf_logged and result.get("cfCalled"):
                logger.info("[debug] confirmFilter 被调用瞬间参数: %s", result.get("cfArgs"))
                cf_logged = True
            if result.get("orderSuccess"):
                logger.info("预约成功(软判据: vm.orderSuccess): %s", result.get("msg", ""))
                return _evidence_verified(page, cfg, "vm.orderSuccess")
            # dry-run 验证：滑块已解决（showCodeCheck 消失）且 freeBook 被拦截
            #   注意：拦截响应是空 body（route.abort()），不是真实 server 响应，
            #   所以这里只能断言「请求确实出去了 + 滑块已通过」，不能误把「请求
            #   未发出」当成成功。
            #
            #   dry-run 的语义是「测试跑通」，不产生真实预约：
            #   - 滑块通过 = showCodeCheck 消失 ✓
            #   - freeBook 真的发出去 = _captured_freebook 非空（requestfailed 事件
            #     记录了被 route.abort() 拦截的请求）✓
            #   - 弹窗 Vue vm 正常 = result.found ✓
            #   这三条都满足 = 真实环境里这一步会成功，dry-run 视为成功（虚拟成功）。
            #   注意：阅读室/座位/时间错配在前置步骤（点击阅览室/座位/弹窗设值）就
            #   已经 fail 了，不会走到这一步；这里一定是流程跑通的最后一步。
            if cfg.get("dry_run") and result.get("found") and not result.get("showCodeCheck"):
                if _captured_freebook:
                    logger.info("[dry-run] 滑块验证已通过，freeBook 已被拦截，未产生真实预约（dry-run 视为虚拟成功）：%s",
                                result)
                    return True
                # showCodeCheck 消失了但 freeBook 没被记录到 → 滑块可能没通过，继续轮询
            # 弹窗关闭且无成功标记，认为失败
            if result.get("found") and not result.get("show") and not result.get("showSelectModal") and not result.get("showCodeCheck"):
                # ★ 核心 bug：response 事件可能比 page 内部异步状态慢一拍，
                # 先用短期等待把响应补回来（最多 3 秒，poll 200ms）。
                wait_count = 0
                while wait_count < 15:  # 15 × 200ms = 3s
                    # 已经看到响应 → 跳出
                    if _captured_freebook:
                        break
                    # 已经看到请求 → 等响应最多 2 秒
                    if _captured_requests:
                        wait_count += 1
                        page.wait_for_timeout(200)
                        continue
                    # 没看到任何 freeBook 网络活动 → 不必等
                    break

                # 排查：读取 freeBook 响应与界面错误提示
                try:
                    diag = page.evaluate("""() => {
                        const msgs = Array.from(document.querySelectorAll('.el-message__content')).map(e=>e.textContent);
                        const app=document.getElementById('app');
                        function rootVM(){ if(app&&app.__vue__)return app.__vue__; const all=document.querySelectorAll('*'); for(const e of all) if(e.__vue__) return e.__vue__; return null; }
                        function walk(vm,acc){ if(!vm)return; if(vm.orderObj) acc.push(vm.orderObj); for(const c of (vm.$children||[])) walk(c,acc);}
                        const arr=[]; walk(rootVM(),arr);
                        return {messages: msgs, orderObjs: arr, hash: location.hash, currentBook: (function(){
                            try { return sessionStorageProxy.getItem('currentBook'); } catch(e){ return null; }
                        })()};
                    }""")
                except Exception as exc:  # noqa: BLE001
                    diag = {"messages": [], "orderObjs": f"read err: {exc}"}
                logger.warning("弹窗已关闭但未成功: %s", result)

                # ★ 关键判据 1：freeBook 响应 body 里的 status=True / code=200
                # 这是最硬的证据（即使 vm.orderSuccess 没及时置 True）。
                for entry in _captured_freebook:
                    body = entry.get("body") if isinstance(entry, dict) else None
                    if not isinstance(body, dict):
                        continue
                    if body.get("status") is True and body.get("code") in (200, "200"):
                        logger.info("预约成功（freeBook 响应: %s）：%s",
                                    body.get("message", ""),
                                    (body.get("data") or {}).get("id") or "")
                        return True

                # ★ 关键判据 1.5：响应里 data.status=RESERVE（也可能是这种格式）
                for entry in _captured_freebook:
                    body = entry.get("body") if isinstance(entry, dict) else None
                    if not isinstance(body, dict):
                        continue
                    data = body.get("data")
                    if isinstance(data, dict) and data.get("status") == "RESERVE" and data.get("id"):
                        logger.info("预约成功（freeBook data.status=RESERVE：id=%s）", data.get("id"))
                        return True

                # ★ 关键判据 1.6：响应是「所选时段已有有效预约」+ data.ctId 非空
                # 济大后端的语义：每个用户每天对某时段只能有一条有效预约。重复请求时
                # 返回 {status: False, code: 500, message: "所选时段已有有效预约",
                #        data: {ctId: <已有订单 id>, id: "", ...}}
                # 此时目标（占住此座位此时段）已经达成，前端弹窗却提示失败、orderSuccess=False
                # —— 用户在系统里能查到这条预约，脚本却误报失败。
                # 用 message 含「已有 / 已预约」+ data.ctId 非空作为软成功信号，
                # 并强制走后台「记录查询」做铁证二次核对，让所有「软判据」可追溯。
                for entry in _captured_freebook:
                    body = entry.get("body") if isinstance(entry, dict) else None
                    if not isinstance(body, dict):
                        continue
                    msg = str(body.get("message", "") or "")
                    data = body.get("data") or {}
                    if isinstance(data, dict) and data.get("ctId") \
                            and ("已有" in msg or "已预约" in msg or "已存在" in msg):
                        logger.info("预约成功(语义成功: ctId=%s, msg=%s)，走后台铁证二次核对",
                                    data.get("ctId"), msg)
                        if _evidence_verified(page, cfg, f"ctId 已有={data.get('ctId')}"):
                            return True
                        # 铁证未命中（可能因为本次重复请求对应的时段已有别的预约，
                        # 但目标时段可能不一致；例如「今天已约」 vs 「明天要约」）。
                        # 这种情况下 ctId 已非空，原软判据仍然有效 —— 视为成功但
                        # 通过 message 包含「已有」+ ctId 是单向硬证据。
                        logger.info("预约成功(半硬判据: ctId=%s, msg=%s; 跳过铁证，仍视为成功)",
                                    data.get("ctId"), msg)
                        return True

                # ★ 关键判据 2：sessionStorageProxy 里的 currentBook 已被 set
                # getCurrentBook() 是在 confirmFilter.then 里调用的，写入 currentBook
                # 说明 freeBook 已成功 + 后端确认返回了订单数据。
                cur_book = (diag or {}).get("currentBook") if isinstance(diag, dict) else None
                if cur_book and cur_book != "null" and cur_book != "{}":
                    logger.info("预约成功(软判据: sessionStorage.currentBook 已写入): %s", cur_book[:200])
                    return _evidence_verified(page, cfg, "currentBook")

                logger.warning("诊断 freeBook响应=%s | 请求=%s | el-message=%s | orderObjs=%s",
                               _captured_freebook, _captured_requests,
                               (diag or {}).get("messages"), (diag or {}).get("orderObjs"))

                # 兜底 1：前端 vm.orderObj 里可能有「已成功」订单（status=RESERVE 表示预约成功）。
                for oo in (diag.get("orderObjs") if isinstance(diag, dict) else None) or []:
                    if not isinstance(oo, dict):
                        continue
                    if oo.get("status") == "RESERVE" and oo.get("id"):
                        logger.info("预约成功(软判据: orderObj=RESERVE id=%s)，走后台铁证二次核对",
                                    oo.get("id"))
                        return _evidence_verified(page, cfg, f"orderObj=RESERVE id={oo.get('id')}")

                # 兜底 2：orderSuccess 轮询可能漏读，改用首页"我的预约"卡片核对是否真的下单成功
                if not cfg.get("dry_run"):
                    make_date_chk = _make_date(cfg.get("date", "tomorrow"))
                    if _reservation_exists(page, make_date_chk, cfg.get("room", "第一阅览室"),
                                           _tw_start, _tw_end):
                        logger.info("预约成功（已通过首页预约记录核对）：%s %s %s~%s",
                                    cfg.get("room"), make_date_chk,
                                    _tw_start, _tw_end)
                        return True
                return False

            # ===== 滑块错误自动重试 =====
            # 若 showCodeCheck 一直 True 且没看到成功的 freeBook 请求 → 滑块可能没通过。
            # 优先级：
            #   1) 服务器自动给了新滑块（img src 变化 或 wrap DOM 重建）→ 直接重新
            #      analyze+拖动；这是 TAC 服务端的标准行为，脚本不该抢着刷。
            #   2) 连续 N 轮服务器都没给新滑块 → 脚本主动调 _reload_tac 强制刷新。
            # dry-run 模式下不重试（拦截的 freeBook 请求永远是 aborted）
            if (result.get("found") and result.get("showCodeCheck")
                    and not cfg.get("dry_run")):
                any_real_freebook = any(
                    isinstance(e, dict) and isinstance(e.get("body"), dict)
                    for e in _captured_freebook
                )
                if any_real_freebook:
                    # 有请求出去过 → 等响应，继续轮询
                    slider_error_streak = 0
                else:
                    slider_error_streak += 1
                    # 1) 优先：服务器是否已经给了新滑块？
                    #    判据：滑块背景图 src 变了，或 showCodeCheck 数值变了（说明前端
                    #    触发了 _captchaReload）。如果变了就**不刷**，重新 analyze。
                    new_slider_visible = page.evaluate("""() => {
                        const w = document.querySelector('#show-code-check-wrap');
                        if (!w) return false;
                        const bg = w.querySelector('#tianai-captcha-slider-bg-img')
                                   || w.querySelector('.bg-img-div img')
                                   || w.querySelector('img[src^="data:image/jpeg"]');
                        const newBg = bg && bg.src ? bg.src.slice(-64) : '';
                        return {newBg, prevBg: window.__lastBgSig || '',
                                showFlag: !!document.querySelector('.showCodeCheck')};
                    }""")
                    if new_slider_visible and isinstance(new_slider_visible, dict) \
                            and new_slider_visible.get("newBg") \
                            and new_slider_visible.get("newBg") != new_slider_visible.get("prevBg") \
                            and new_slider_visible.get("prevBg"):
                        # 服务器给的新滑块到位 → 不刷，直接重试
                        slider_error_streak = 0
                        page.evaluate("""(sig) => { window.__lastBgSig = sig; }""",
                                      new_slider_visible["newBg"])
                        logger.info("服务器已自动给新滑块（背景图 src 变化），直接重试 analyze")
                        try:
                            slv.solve(page, cfg)
                            logger.info("滑块重试完成（服务器刷新版）")
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("滑块重试 solve 异常: %s", exc)
                        page.wait_for_timeout(300)
                        continue  # 不刷，继续轮询
                    # 记录当前 bg sig，下一轮用来对比
                    page.evaluate("""(sig) => { window.__lastBgSig = sig; }""",
                                  (new_slider_visible or {}).get("newBg") if isinstance(new_slider_visible, dict) else "")

                    # 2) 5 轮（约 2.5 秒）服务器都没给新滑块 → 脚本主动刷
                    SLIDER_ERROR_LIMIT = 5
                    SLIDER_ERROR_RETRY = 2
                    if (slider_error_streak >= SLIDER_ERROR_LIMIT
                            and slider_retried < SLIDER_ERROR_RETRY):
                        slider_retried += 1
                        slider_error_streak = 0
                        logger.warning("滑块连续 %d 轮服务器未刷新（无新 bg src 变化、无 freeBook 请求），"
                                       "主动调 _reload_tac 强制刷新（第 %d/%d 次）",
                                       SLIDER_ERROR_LIMIT, slider_retried, SLIDER_ERROR_RETRY)
                        from src.slider import _reload_tac as _slider_reload
                        _slider_reload(page)
                        page.wait_for_timeout(1000)
                        # 重新触发 codeCheck 让新验证码出现
                        page.evaluate(f"""() => {{{JS_DIALOG_ACTION} if(vm) {{ try{{ vm.showCodeCheck=true; vm.codeCheck(); }}catch(e){{}} }} return !!vm; }}""")
                        _wait_for(page, """() => {
                            const w = document.querySelector('#show-code-check-wrap');
                            if (w && getComputedStyle(w).display!=='none' && w.getBoundingClientRect().width>0) return true;
                            const bg = document.querySelector('#tianai-captcha-slider-bg-img');
                            return !!(bg && bg.getBoundingClientRect().width>0);
                        }""", timeout_ms=4000, poll_ms=200)
                        try:
                            slv.solve(page, cfg)
                            logger.info("滑块重试完成（脚本主动刷新版）")
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("滑块重试 solve 异常: %s", exc)

            page.wait_for_timeout(500)

        logger.warning("预约结果超时或失败: %s", result)
        # 兜底：同上，用首页预约记录核对
        if not cfg.get("dry_run"):
            make_date_chk = _make_date(cfg.get("date", "tomorrow"))
            if _reservation_exists(page, make_date_chk, cfg.get("room", "第一阅览室"),
                                   _tw_start, _tw_end):
                logger.info("预约成功（已通过首页预约记录核对）：%s %s %s~%s",
                            cfg.get("room"), make_date_chk,
                            _tw_start, _tw_end)
                return True
        return False
    finally:
        # owns_session=True 时由本函数负责关闭（外部直接 book_once 入口）；
        # owns_session=False 时由调用方负责关闭（GUI 预热缓存复用场景）。
        if owns_session and not cfg.get("debug") and sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass


def _book_via_browser(cfg: Config) -> bool:
    """传统入口：内部启动登录浏览器并跑预约流程。"""
    sess = br.login_browser(cfg)
    try:
        return _book_via_browser_on_page(sess.page, cfg, owns_session=False, sess=sess)
    finally:
        if not cfg.get("debug"):
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass


def book_once_with_page(page, cfg: Config, sess=None) -> bool:
    """对外公开接口：在已登录的 page 上跑预约。

    GUI / 定时守护在预热登录后保留 `BrowserSession`，按下「开始预约」时直接复用
    BrowserSession.page，可省去 pw.launch + new_context + 12s 轮询的
    ~18 秒冷启动时间。session 的生命周期由调用方自己管理。

    sess 可选：传了就先 `ensure_home()`。复用的页面通常停在 SSO 登录后的
    跳转页（projectIndex），不回首页的话后面选座会找不到房间卡片。
    页面已在首页时该调用立即返回，不产生额外耗时。
    """
    mode = cfg.mode
    _tw_s, _tw_e = cfg.time_window()
    logger.info("开始预约，模式=%s，用户=%s，阅览室=%s 座位%s %s~%s 日期=%s",
                mode, cfg.username, cfg.get("room"), cfg.get("seat"),
                _tw_s, _tw_e, cfg.get("date"))
    if sess is not None:
        try:
            sess.ensure_home()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ensure_home 失败（继续尝试选座）: %s", exc)
    if mode == "browser":
        return _book_via_browser_on_page(page, cfg, owns_session=False, sess=None)
    # api / auto 模式不强制依赖浏览器（auto 会回落到 browser）；
    # 但目前 ui 的 cfg.mode 一律是 browser，下方分支为兜底。
    if _book_via_api(cfg):
        return True
    logger.info("book_once_with_page 自动模式：API 失败，降级到浏览器模式")
    return _book_via_browser_on_page(page, cfg, owns_session=False, sess=None)


def book_once(cfg: Config) -> bool:
    """执行一次预约。根据 mode 自动选择路径。"""
    mode = cfg.mode
    _tw_s, _tw_e = cfg.time_window()
    logger.info("开始预约，模式=%s，用户=%s，阅览室=%s 座位%s %s~%s 日期=%s",
                mode, cfg.username, cfg.get("room"), cfg.get("seat"),
                _tw_s, _tw_e, cfg.get("date"))

    if mode == "api":
        return _book_via_api(cfg)

    if mode == "browser":
        return _book_via_browser(cfg)

    # auto
    if _book_via_api(cfg):
        return True
    logger.info("auto 模式：API 失败，降级到浏览器模式")
    return _book_via_browser(cfg)


def _shift_hhmm(hhmm: str, minutes: int) -> str:
    """把 HH:MM 平移若干分钟（支持跨天，返回 HH:MM）。"""
    h, m = hhmm.split(":")
    base = datetime.now().replace(hour=int(h), minute=int(m),
                                  second=0, microsecond=0)
    return (base + timedelta(minutes=minutes)).strftime("%H:%M")


def _wait_until_next(hhmm: str, label: str):
    """阻塞到「下一次」HH:MM（今天该时刻已过则等到明天），避免守护循环空转。"""
    now = datetime.now()
    h, m = hhmm.split(":")
    target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    secs = (target - now).total_seconds()
    logger.info("%s：等待 %.0f 秒至 %s", label, secs,
                target.strftime("%m-%d %H:%M"))
    time.sleep(secs)


def _wait_network_ready(host: str = "seat.ujn.edu.cn", port: int = 443,
                        timeout: float = 90.0) -> bool:
    """唤醒后等网络真正连上再开始登录。

    电脑从睡眠唤醒时 Wi-Fi 重新关联通常要几秒，这时候直接开浏览器会卡在
    DNS / 连接超时上，白白吃掉宝贵的预热窗口。这里先探一次 TCP 443。
    """
    import socket
    deadline = time.time() + timeout
    waited = 0.0
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=4):
                if waited:
                    logger.info("网络已就绪（唤醒后等待 %.1f 秒）", waited)
                return True
        except OSError:
            pass
        time.sleep(2.0)
        waited += 2.0
    logger.warning("等待网络就绪超时（%.0f 秒），仍尝试继续登录", timeout)
    return False


def serve(cfg: Config, open_time: str = "07:00", prewarm_minutes: int = 2,
          keep_awake: bool = True, max_late_minutes: int = 30,
          run_once: bool = False, check_network: bool = True,
          max_wait_minutes: int = 60):
    """定时守护：每天 open_time 准点抢座，提前 prewarm_minutes 分钟预热登录。

    三条关键设计（对应"挂机等到早上 7 点"的真实场景）：

    1. **预热保活，而非预热后关闭**
       旧版预热登录完就 `sess.close()`，准点再重新 `login_browser` —— 等于
       把 8~10 秒的冷启动又花了一遍。新版预热后**保持 BrowserSession 不关**，
       准点直接用 `book_once_with_page(sess.page, cfg)` 提交，省掉
       `pw.launch + new_context + 登录轮询` 约 9 秒。
       （CLI 全程单线程，不存在 Playwright 跨线程问题。）

    2. **等待期间阻止系统睡眠**
       系统一睡，`time.sleep()` 就不推进，早上 7 点根本不会触发。用
       `src.wake.WakeLock` 在等待期间申请"别睡"，抢完立刻释放恢复正常电源策略。

    3. **分片等待 + 睡过头判定**
       见 `src/scheduler.py`：每 0.05~20 秒重新对齐墙上时钟，电脑中途睡了也能
       醒来立刻补跑；但迟到超过 max_late_minutes 分钟（说明睡了大半天）就跳过
       当天，不在奇怪的时间补跑。
    """
    from . import scheduler as _sched
    from .wake import WakeLock

    state = {"sess": None, "last_check": 0.0}

    def _do_login():
        sess = br.login_browser(cfg)
        state["sess"] = sess
        return sess

    def on_prewarm():
        if check_network:
            _wait_network_ready()
        t0 = time.time()
        sess = _do_login()
        # ★ 预热阶段就把页面停到首页并等房间卡片渲染好。
        #   否则准点提交时才 goto_home，白白把 1~8 秒的渲染等待留到开放之后。
        try:
            if sess.ensure_home():
                logger.debug("预热页面已停在首页，房间卡片已渲染")
            else:
                logger.warning("预热页面未能回到首页，准点提交时会再试一次")
        except Exception as exc:  # noqa: BLE001
            logger.debug("预热 ensure_home 异常: %s", exc)
        logger.info("预热登录完成（%.1f 秒）：页面保持等待，准点直接提交，"
                    "省去重新冷启动约 9 秒", time.time() - t0)
        return sess

    def _close_all(extra=None):
        """关掉并丢弃所有持有的 BrowserSession（去重，避免重复 close）。"""
        seen = set()
        for s in (extra, state.get("sess")):
            if s is None or id(s) in seen:
                continue
            seen.add(id(s))
            try:
                # 先摘掉 dry-run 的 freeBook 拦截再关页面：否则 playwright 会在
                # 关闭的瞬间抛一串 asyncio.CancelledError（无害，但日志很吓人）
                page = getattr(s, "page", None)
                if page is not None:
                    try:
                        page.unroute_all()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        state["sess"] = None

    def on_heartbeat(remaining, phase):
        """等待期间每片调用；每 30 秒检查一次预热页面是否还活着，挂了就重登。"""
        if state["sess"] is None:
            return None
        now = time.time()
        if now - state["last_check"] < 30.0:
            return None
        state["last_check"] = now
        try:
            # ★ alive 是 property，不是方法（写成 alive() 会 'bool' object is not callable）
            if not state["sess"].alive:
                logger.warning("预热页面意外失效，立即重新登录保活 ...")
                # 必须先彻底释放旧实例：同一线程里起第二个 sync_playwright 会报
                # "It looks like you are using Playwright Sync API inside the asyncio loop"
                _close_all()
                _do_login()
        except Exception as exc:  # noqa: BLE001
            logger.debug("保活检查异常: %s", exc)
        return None

    def on_book(sess):
        if sess is None:
            sess = state.get("sess")
        ok = False
        try:
            alive = False
            if sess is not None:
                try:
                    # ★ alive 是 property，不是方法
                    alive = bool(sess.alive)
                except Exception:  # noqa: BLE001
                    alive = False
            if alive:
                logger.info("复用预热页面提交（跳过登录，直奔选座）")
                ok = book_once_with_page(sess.page, cfg, sess=sess)
            else:
                logger.warning("预热页面不可用，改为完整登录流程（慢约 10 秒）")
                _close_all(sess)  # 同上：先释放旧实例再重新登录
                ok = book_once(cfg)
        finally:
            # 提交完立刻关掉预热浏览器，别让 Edge 挂一整天
            _close_all(sess)
        return ok

    sched = _sched.DailyScheduler(
        open_time=open_time,
        prewarm_minutes=prewarm_minutes,
        max_late_minutes=max_late_minutes,
        max_wait_minutes=max_wait_minutes,
        on_prewarm=on_prewarm,
        on_heartbeat=on_heartbeat,
        on_book=on_book,
    )
    lock = WakeLock() if keep_awake else None
    if lock is not None and not lock.supported:
        logger.warning("当前平台无法阻止系统睡眠：%s", lock.describe())
    try:
        if lock is not None:
            lock.acquire()
        return sched.run(run_once=run_once)
    finally:
        s = state.get("sess")
        if s is not None and not cfg.get("debug"):
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        if lock is not None:
            lock.release()
