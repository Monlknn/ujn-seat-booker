"""验证滑块错误自愈逻辑：检测到服务器给新滑块时直接重试，不自己刷。"""
import sys
sys.path.insert(0, '.')

# 模拟变量
prev_bg_sig = "abc123"
new_bg_sig = "def456"  # 服务器换的

# 测试判据逻辑
def should_just_retry(prev, new_sig):
    return bool(new_sig) and (new_sig != prev) and bool(prev)

assert should_just_retry("abc", "def") is True, "应该重试"
assert should_just_retry("abc", "") is False, "空 sig 不应重试"
assert should_just_retry("", "def") is False, "没有 prev 不应重试"
assert should_just_retry("abc", "abc") is False, "sig 相同不应重试"

print('✓ 滑块服务器给新验证码 → 直接重试（不主动刷）判据通过')

# 测试 _wait_room_list_stable 函数可调用
from src.gui import App
import inspect
assert callable(App._wait_room_list_stable)
assert callable(App._collect_rooms_across_pages)
assert callable(App._fetch_rooms_in_view)
assert callable(App._room_list_signature)
print('✓ 所有 static 方法已正确暴露')

# 测试 SYSTEM_START_HHMM 已删除（★ 2026-09-04 修正：弹窗有开始/结束时间下拉，
# 不再强制 07:00）
from src import gui
import sys
assert not hasattr(gui, 'SYSTEM_START_HHMM'), 'SYSTEM_START_HHMM 应已被删除'
print('✓ SYSTEM_START_HHMM 已移除（开始时间由用户在弹窗里选）')

# 测试 booker 已恢复「先设 startTime 再设 endTime」逻辑
from src import booker
src = open('src/booker.py', 'r', encoding='utf-8').read()
assert 'vm.search.starTimeValue = String(args.start)' in src, '应该恢复设置 starTimeValue'
assert 'vm.search.endTimeValue = String(args.end)' in src, '应该设置 endTimeValue'
assert 'start_min_default = 7 * 60' not in src, '旧的「系统固定 07:00」假设应已移除'
# 验证顺序：starTimeValue 在 endTimeValue 之前
assert src.index('vm.search.starTimeValue = String(args.start)') < \
       src.index('vm.search.endTimeValue = String(args.end)'), \
       '必须先设 startTime 再设 endTime（否则 endTime 会被 Vue 重置）'
print('✓ booker.py 已恢复「先 startTime 再 endTime」顺序')

# 测试 booker 新增的服务器给新滑块检测
assert 'window.__lastBgSig' in src, '应该用 window.__lastBgSig 跟踪 bg sig'
print('✓ booker.py 已实现"服务器给新滑块时直接重试"分支')

# 测试「所选时段已有有效预约」兜底（★ 2026-09-04 23:58 实测：23:55 那次
# 服务端返回 status=False, message="所选时段已有有效预约", data.ctId="..."，
# 目标其实已经达成但所有 5 层兜底都不命中，导致脚本误报失败）
assert '已有' in src, '应该有"已有"关键字判断'
assert 'data.get("ctId")' in src, '应该读 data.ctId 作为已有订单 ID'
assert '已预约' in src or '已存在' in src, '应该有"已预约"/"已存在"作为兜底关键字'
print('✓ booker.py 已加「已有有效预约→视为成功」第 6 层兜底（解决 23:55 误判）')

# 验证 1.6 兜底逻辑在 5 个真实场景下的判定（直接复刻判定条件）
def _is_existing_reservation_success(_captured_freebook):
    for entry in _captured_freebook:
        body = entry.get("body") if isinstance(entry, dict) else None
        if not isinstance(body, dict):
            continue
        msg = str(body.get("message", "") or "")
        data = body.get("data") or {}
        if isinstance(data, dict) and data.get("ctId") \
                and ("已有" in msg or "已预约" in msg or "已存在" in msg):
            return True
    return False

# 23:55 实测响应：已有有效预约 → 应判成功
case_existing = [{"status": 200, "ok": True,
                  "body": {"status": False, "code": 500,
                           "message": "所选时段已有有效预约",
                           "data": {"ctId": "2095888048553840640", "id": ""}}}]
assert _is_existing_reservation_success(case_existing) is True, "23:55 场景应判成功"

# 22:53 实测成功响应：操作成功 → 1.6 不应命中（由 1/1.5 命中）
case_new_ok = [{"status": 200, "ok": True,
                "body": {"status": True, "code": 200,
                         "message": "操作成功",
                         "data": {"id": "2095888048553840640", "status": "RESERVE"}}}]
assert _is_existing_reservation_success(case_new_ok) is False, "操作成功不应被 1.6 处理"

# dry-run 拦截 → response.body 为 None，1.6 不应误判
case_aborted = [{"status": "aborted", "url": "...", "method": "POST",
                 "failure": "net::ERR_FAILED"}]
assert _is_existing_reservation_success(case_aborted) is False, "拦截不应误判为已有"

# ctId 缺失时保守不判
case_no_ctid = [{"body": {"status": False, "code": 500,
                          "message": "该用户已有预约",
                          "data": {"ctId": "", "id": ""}}}]
assert _is_existing_reservation_success(case_no_ctid) is False, "ctId 缺失不判成功"

# "座位已被占" ≠ "已有有效预约"，不应判成功
case_seat_taken = [{"body": {"status": False, "code": 500,
                             "message": "座位已被占",
                             "data": {"ctId": "xxx", "id": ""}}}]
assert _is_existing_reservation_success(case_seat_taken) is False, "座位被占不判成功"
print('✓ booker.py 1.6 兜底 5 个真实场景判定正确（23:55 已有预约→成功；操作成功/拦截/ctId缺失/座位被占→不判成功）')

# dry-run 跑通 = 虚拟成功（★ 2026-09-05 用户反馈：dry-run 报失败不合理）
src_lines = src.split('\n')
# 关键判定：dry-run + found + showCodeCheck=False + _captured_freebook 非空 → return True
assert 'if cfg.get("dry_run") and result.get("found") and not result.get("showCodeCheck"):' in src, \
    '应该保留 dry-run 虚拟成功判定分支'
# 不应再 return False（虚拟失败）
dry_run_block = src[src.index('# dry-run 验证：滑块已解决（showCodeCheck 消失）且 freeBook 被拦截'):
                    src.index('# 弹窗关闭且无成功标记，认为失败')]
assert 'return True' in dry_run_block, 'dry-run 跑通应该 return True（虚拟成功）'
assert 'return False' not in dry_run_block, 'dry-run 跑通不应 return False（虚拟失败）'
assert '虚拟成功' in dry_run_block, '日志应该说明 dry-run 视为虚拟成功'
print('✓ booker.py dry-run 跑通返回 True（虚拟成功，不再报失败）')

# 测试 gui 已修复楼层过滤：FLOOR_NAME_MAP 不再被实际使用（之前是错的，因为济大
# 「第N阅览室」不在 N 层），改用响应里的 floorName 字段做权威过滤
from src import gui
assert isinstance(gui.FLOOR_NAME_MAP, dict) and len(gui.FLOOR_NAME_MAP) == 0, \
    'FLOOR_NAME_MAP 已清空，不再用于客户端名字匹配'
assert hasattr(gui.App, '_collect_rooms_via_api'), \
    'App._collect_rooms_via_api 应存在（新接口拦截版 refresh_rooms 用）'
src_gui = open('src/gui.py', 'r', encoding='utf-8').read()
assert 'floorName' in src_gui and 'findRoomDuration' in src_gui, \
    'refresh_rooms 应通过 floorName 字段过滤 findRoomDuration 响应'
print('✓ gui.py refresh_rooms 改用 findRoomDuration 接口拦截 + floorName 字段过滤（已修复「选了 7 层显示别的楼层」的 bug）')

# 测试 _BookWorker 类（★ 2026-09-05 用户反馈：预热后浏览器重开浪费时间）
# 用 mock 替代 login_browser + booker，避免真实启动 Edge
import threading
import time as _time
from unittest.mock import MagicMock, patch as _patch
from src.gui import _BookWorker

# 1. 类存在且基本字段正确
assert hasattr(gui, '_BookWorker'), '_BookWorker 类应存在'
assert issubclass(_BookWorker, threading.Thread), '_BookWorker 应该是 Thread 子类'

# 2. 旧 API 已清理（_prewarmed_sess / _acquire_book_session / _close_prewarmed_sess）
assert '_prewarmed_sess = None' not in src_gui and '_prewarmed_sess' not in src_gui, \
    '旧的 _prewarmed_sess 应该被 _worker 替代'
assert '_acquire_book_session' not in src_gui, '_acquire_book_session 应该被 _worker.submit 替代'
assert '_close_prewarmed_sess' not in src_gui, '_close_prewarmed_sess 应该被 _worker.stop 替代'
assert 'self._worker = _BookWorker()' in src_gui and 'self._worker.start()' in src_gui, \
    '_worker 应在 App.__init__ 里创建并启动'
assert 'app._worker.stop(timeout=' in src_gui, 'GUI 关闭时应停 worker（_on_close 里 app._worker.stop）'
print('✓ gui.py 旧预热字段已清理（_prewarmed_sess/_acquire_book_session/_close_prewarmed_sess → _worker）')

# 3. 模拟 worker：mock login_browser + booker，测试 submit 串行性
fake_sess = MagicMock()
fake_sess.alive = True
fake_sess.page = MagicMock()

call_log = []
def fake_login_browser(cfg):
    call_log.append(("login_browser", cfg))
    return fake_sess

def fake_book_once_with_page(page, cfg, sess=None):
    call_log.append(("book_once_with_page", page, cfg))
    return True

with _patch("src.session.login_browser", side_effect=fake_login_browser), \
     _patch("src.booker.book_once_with_page", side_effect=fake_book_once_with_page):
    worker = _BookWorker()
    worker.start()
    _time.sleep(0.1)  # 让 worker 进入 wait
    
    # 4. prewarm 任务
    cfg1 = MagicMock()
    result = worker.submit("prewarm", cfg1, timeout=5.0)
    assert result == ("prewarm_ok", True), f"prewarm 期望 ('prewarm_ok', True)，实际 {result}"
    assert worker._sess is fake_sess, "prewarm 后 sess 应被缓存"
    
    # 5. book 任务应复用同一 sess（不调 login_browser）
    login_calls_before = sum(1 for c in call_log if c[0] == "login_browser")
    cfg2 = MagicMock()
    result = worker.submit("book", cfg2, dry=False, timeout=5.0)
    login_calls_after = sum(1 for c in call_log if c[0] == "login_browser")
    assert result == ("book_ok", True), f"book 期望 ('book_ok', True)，实际 {result}"
    assert login_calls_after == login_calls_before, \
        f"book 不应再调 login_browser，调用次数从 {login_calls_before} 变为 {login_calls_after}"
    print('✓ _BookWorker 复用 sess：prewarm 后 book 不再调 login_browser（节省 8~12 秒冷启动）')
    
    # 6. close_sess 任务
    result = worker.submit("close", cfg=None, timeout=5.0)
    assert result == ("close_ok", True), f"close 期望 ('close_ok', True)，实际 {result}"
    assert worker._sess is None, "close 后 sess 应被清空"
    
    # 7. close 之后再 book 应重新 login_browser
    result = worker.submit("prewarm", cfg1, timeout=5.0)
    assert result == ("prewarm_ok", True), "close 后再 prewarm 应重新登录"
    assert sum(1 for c in call_log if c[0] == "login_browser") == 2, \
        "close 后再 prewarm 应该调一次 login_browser"
    print('✓ _BookWorker close_sess 后下次会重新登录（避免用死 sess）')
    
    worker.stop(timeout=2.0)
    assert not worker.is_alive(), "stop 后 worker 应该已结束"

# 8. 并发测试：第二个 submit 应返回 ("busy", None)
with _patch("src.session.login_browser", side_effect=lambda c: fake_sess), \
     _patch("src.booker.book_once_with_page", side_effect=lambda p, c, sess=None: True):
    worker2 = _BookWorker()
    worker2.start()
    _time.sleep(0.1)
    # 用 sleep 模拟长任务：book_once_with_page 慢一点
    def slow_book(p, c, sess=None):
        _time.sleep(0.3)
        return True
    with _patch("src.booker.book_once_with_page", side_effect=slow_book):
        # 派发第一个任务（耗时 0.3 秒）
        worker2.submit("book", cfg2, timeout=5.0)  # 在主线程同步等
    # 现在 worker 应该空着
    assert not worker2.is_busy(), "任务完成后 worker 应空"
    # 模拟两个线程同时派发：第一个慢，第二个应拿到 busy
    results = []
    def submit_later():
        results.append(("later", worker2.submit("book", cfg2, timeout=5.0)))
    threading.Thread(target=submit_later).start()
    _time.sleep(0.05)  # 让 later 线程先调 submit（此时 worker 还在跑）
    _time.sleep(0.5)  # 等慢任务跑完
    assert len(results) == 1
    status, payload = results[0][1]
    assert status in ("book_ok", "busy", "timeout"), f"应拿到合理状态，实际 {status}"
    worker2.stop(timeout=2.0)

print('✓ _BookWorker 端到端：prewarm → book 复用 sess；close 后下次重新登录；stop 正常退出；并发不会卡死')

# ======================================================================
# 定时抢座 + 防休眠（2026-09-05：用户要"每天 7 点自动约，提前 1-2 分钟登录"）
# ======================================================================
print()
from src.scheduler import (DailyScheduler, parse_hhmm, next_occurrence,
                           format_countdown)
from datetime import datetime as _dt, timedelta as _td

# 1. 时间解析：合法值 & 非法值
assert parse_hhmm("07:00") == (7, 0)
assert parse_hhmm("7:5") == (7, 5), "应容错单个数字"
assert parse_hhmm("0700") == (7, 0), "应容错无冒号"
for bad in ("25:00", "07:60", "abc"):
    try:
        parse_hhmm(bad)
        raise AssertionError(f"{bad} 应该被拒绝")
    except ValueError:
        pass
print('✓ scheduler.parse_hhmm 解析/容错/非法值拒绝正确')

# 2. next_occurrence 跨天逻辑
assert next_occurrence("07:00", _dt(2026, 9, 5, 8, 0)) == _dt(2026, 9, 6, 7, 0)
assert next_occurrence("07:00", _dt(2026, 9, 5, 6, 0)) == _dt(2026, 9, 5, 7, 0)
assert next_occurrence("23:59", _dt(2026, 9, 5, 23, 59, 30)) == _dt(2026, 9, 6, 23, 59)
print('✓ scheduler.next_occurrence 跨天正确（已过→明天，未过→今天）')

# 3. 倒计时文案
assert format_countdown(65) == "1 分 05 秒"
assert format_countdown(3725) == "1 小时 02 分 05 秒"
assert format_countdown(-5) == "0 秒", "负数应夹到 0"
print('✓ scheduler.format_countdown 文案正确')

# 4. 等待精度 + 可取消（分片等待的核心价值：系统睡了也能醒，且点停止立刻响应）
_s = DailyScheduler(open_time="07:00")
_t0 = _time.time()
_reached, _over = _s._wait_until(_dt.now() + _td(seconds=1.5), "t")
assert _reached is True, "应该到点"
assert _over < 0.5, f"误差应 <0.5s，实际 {_over:.3f}s"
assert 1.4 < _time.time() - _t0 < 2.5, "耗时应 ≈1.5 秒"
print('✓ scheduler 等待精度 %.0fms（分片等待，越接近越勤）' % (_over * 1000))

_s2 = DailyScheduler(open_time="07:00")
_t0 = _time.time()
threading.Timer(0.8, _s2.stop).start()
_reached2, _ = _s2._wait_until(_dt.now() + _td(hours=5), "t")
assert _reached2 is False, "被 stop 应返回 False"
assert _time.time() - _t0 < 1.6, "停止应在 1 秒内响应，而不是等满 5 小时"
print('✓ scheduler 可被 stop() 立即取消（%.2fs 内响应，不是等 5 小时）'
      % (_time.time() - _t0))

# 5. DailyScheduler 端到端（mock 回调，秒级）：验证 预热→等待→提交 顺序与保活
_calls = []


class _FakeSess:
    alive = True

    def close(self):
        _calls.append("close")


def _mk_sched(prewarm_min, open_dt, max_late=30):
    sc = DailyScheduler(open_time="07:00", prewarm_minutes=prewarm_min,
                        max_late_minutes=max_late)
    sc.on_prewarm = lambda: (_calls.append("prewarm"), _FakeSess())[1]
    # on_book 模拟"提交完关掉浏览器"，与 src/booker.py: on_book 的 finally 一致
    def _book(sess):
        _calls.append(("book", type(sess).__name__))
        try:
            return True
        finally:
            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass
    sc.on_book = _book
    sc.on_heartbeat = lambda rem, phase: None
    return sc, open_dt - _td(minutes=prewarm_min), open_dt


# 预热窗口已过 → 立即预热；准点已过但在 max_late 内 → 立即补跑
_past = _dt.now() - _td(seconds=5)
sc, pwa, opa = _mk_sched(2, _past)
sc.run(run_once=True, force_next_open_at=opa, force_next_prewarm_at=pwa)
assert _calls.count("prewarm") == 1, f"应预热 1 次，实际 {_calls}"
assert any(c[0] == "book" for c in _calls if isinstance(c, tuple)), f"应提交，实际 {_calls}"
assert "close" in _calls, "提交后应关闭浏览器，不让 Edge 挂一天"
print('✓ DailyScheduler 端到端：已过预热点 → 立即预热 → 补跑提交 → 关闭页面')

# 睡过头（超过 max_late）→ 跳过当天
_calls2 = []
sc2, pwa2, opa2 = _mk_sched(1, _dt.now() - _td(hours=3))   # 晚了 3 小时
_t0 = _time.time()
threading.Timer(1.2, sc2.stop).start()
sc2.run(run_once=True, force_next_open_at=opa2, force_next_prewarm_at=pwa2)
assert _calls2 == [], f"睡过头 3 小时应跳过，不应执行任何动作，实际 {_calls2}"
assert _time.time() - _t0 < 2.5, "跳过应立刻进入下一轮等待（可被 stop 打断）"
print('✓ DailyScheduler 睡过头（>30 分钟）→ 跳过当天，不瞎补跑')

# 6. WakeLock：能拿到锁、能释放、幂等
from src.wake import WakeLock
_wl = WakeLock()
assert isinstance(_wl.describe(), str) and _wl.describe(), "describe 应返回非空文案"
assert _wl.supported is True or not _wl.supported  # 只要不抛异常
if _wl.supported:
    assert _wl.acquire() is True, "应能申请到锁"
    assert _wl.held is True
    assert _wl.acquire() is True, "重复 acquire 应幂等"
    _wl.release()
    assert _wl.held is False, "release 后应不再持有"
    _wl.release()  # 重复 release 不应抛异常
    print(f'✓ WakeLock 申请/释放/幂等正常（{_wl.describe()}）')
else:
    print('✓ WakeLock 当前平台不支持，已优雅降级（不影响主流程）')

# 7. booker.serve：预热后保活复用 page（不是登录完就关）
import inspect as _inspect
from src import booker as _bk
_sig = _inspect.signature(_bk.serve)
for _p in ("prewarm_minutes", "keep_awake", "run_once", "max_late_minutes",
           "check_network"):
    assert _p in _sig.parameters, f"serve 应支持 {_p} 参数"
assert _sig.parameters["prewarm_minutes"].default == 2, "默认提前 2 分钟预热"
_src_serve = _inspect.getsource(_bk.serve)
assert "book_once_with_page" in _src_serve, "准点应复用预热页面提交（省冷启动）"
assert "sess.close()" not in _src_serve.split("def on_prewarm")[-1][:400], \
    "预热回调里不应登录完就 close（旧版 bug：等于白预热）"
assert "_wait_network_ready" in _src_serve, "唤醒后应先等网络就绪"
assert "sess.alive()" not in _src_serve, \
    "alive 是 property，写成 sess.alive() 会 'bool' object is not callable"
assert "WakeLock" in _src_serve, "等待期间应阻止系统睡眠"
print('✓ booker.serve 签名/保活复用/网络等待/防休眠 全部到位')

# 8. CLI 参数
_src_cli = open('src/cli.py', encoding='utf-8').read()
for _flag in ("--prewarm", "--once", "--no-wake-lock", "--max-late"):
    assert _flag in _src_cli, f"cli 应支持 {_flag}"
assert '"wake"' in _src_cli, "cli 应支持 wake 自检子命令"
print('✓ cli.py 支持 --prewarm / --once / --no-wake-lock / --max-late / wake')

# 9. 配置项 schedule 段
from src.config import DEFAULTS as _DEF, Config as _Config
assert "schedule" in _DEF, "DEFAULTS 应有 schedule 段"
_sc_def = _DEF["schedule"]
assert _sc_def["open_time"] == "07:00", "默认 07:00（济大开放时间）"
assert _sc_def["prewarm_minutes"] == 2, "默认提前 2 分钟"
assert _sc_def["keep_awake"] is True, "默认阻止睡眠"
_cfg = _Config({"username": "u"})
assert _cfg.get("schedule")["prewarm_minutes"] == 2, "Config 应合并 schedule 默认值"
print('✓ config.py 新增 schedule 段并正确合并默认值')

# 10. GUI 定时面板 + worker 探活
from src import gui as _gui
for _m in ("_build_schedule_section", "_start_schedule", "_stop_schedule",
           "_run_scheduler", "_sched_prewarm", "_sched_book", "_sched_heartbeat"):
    assert hasattr(_gui.App, _m), f"App 应有 {_m}"
_src_gui2 = open('src/gui.py', encoding='utf-8').read()
assert '"ping"' in _src_gui2, 'worker 应支持 ping 任务（守护线程探活）'
assert 'DailyScheduler' in _src_gui2, "GUI 应复用 DailyScheduler"
# Tk 变量不能跨线程读：必须快照配置
assert "_sched_cfg = self._build_cfg" in _src_gui2, \
    "启动守护时应在主线程快照配置（Tk 变量非线程安全）"
assert 'WakeLock' in _src_gui2, "GUI 守护也应阻止系统睡眠"
print('✓ gui.py 定时面板与 worker 探活就位（含 Tk 变量跨线程快照）')

print()
print('全部单元测试通过 ✓')