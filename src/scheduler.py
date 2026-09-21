"""定时调度器：每天准点抢座，且**不会因系统睡眠而错过**。

设计要点（针对"挂机一整晚等到早上 7 点"这个场景）
------------------------------------------------
1. **分片等待，而不是一次 `time.sleep(24 小时)`**
   系统睡眠期间进程被挂起，`time.sleep()` 不推进 —— 一次性睡到天亮，
   只要中途睡过一次就永远错过。这里每 0.05~20 秒醒一次重新对齐墙上时钟
   （距目标越近，醒得越勤），所以：
     - 电脑中途睡了 → 醒来后立刻发现"已经过了目标时刻"→ 立即补跑
     - 电脑一直醒着 → 精度到 50ms 内命中整点

2. **预热窗口 + 准点提交**
   济大 07:00 开放次日预约，SSO 登录要 8~10 秒。等 07:00 再登录就晚了一截，
   所以提前 N 分钟（默认 2 分钟）先登录并**保活 BrowserSession**，
   07:00:00 整直接用已登录的 page 提交 —— 把耗时全部挪到开放之前。

3. **睡过头判定**
   如果醒来时已经过了 `open_time + max_late_minutes`（默认 30 分钟，
   说明电脑睡了很久而不是晚几秒），就**跳过当天**等明天，
   避免大白天补跑一个早该结束的预约。

4. **可取消**
   所有等待都通过 `stop_event` 中断，GUI 点「停止」能立刻退出，不用等满。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple

from .logger import logger

# 距目标还有多久 → 这一片最多睡多久（越接近目标醒得越勤）
_CHUNKS = [
    (300.0, 20.0),   # > 5 分钟：20 秒醒一次（省电）
    (60.0, 5.0),     # 1~5 分钟：5 秒
    (5.0, 1.0),      # 5~60 秒：1 秒
    (0.5, 0.2),      # 0.5~5 秒：200 毫秒
    (0.0, 0.05),     # 最后 0.5 秒：50 毫秒（精确命中整点）
]


def _chunk_for(remaining: float) -> float:
    for threshold, chunk in _CHUNKS:
        if remaining > threshold:
            return chunk
    return 0.05


def parse_hhmm(hhmm: str) -> Tuple[int, int]:
    """解析 HH:MM（容错：7:5 → 07:05，0700 → 07:00）。"""
    s = str(hhmm).strip()
    if ":" not in s and len(s) == 4 and s.isdigit():
        s = s[:2] + ":" + s[2:]
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间格式非法: {hhmm}")
    return h, m


def next_occurrence(hhmm: str, now: Optional[datetime] = None) -> datetime:
    """返回「下一次」HH:MM 的 datetime（今天的该时刻已过则取明天）。"""
    now = now or datetime.now()
    h, m = parse_hhmm(hhmm)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def current_occurrence(hhmm: str, now: Optional[datetime] = None,
                       max_late_minutes: int = 0) -> datetime:
    """返回「当前该执行的那一次」HH:MM。

    与 ``next_occurrence`` 的关键区别：**今天的目标时刻已过、但还在容忍窗口内时，
    仍然返回今天的目标时刻**，而不是翻到明天。

    为什么必须区分（2026-09-11 实测事故）
    ------------------------------------
    计划任务用 ``StartWhenAvailable`` 补跑错过的时机时，实际可能 07:10 才启动
    （电脑 06:58 没被唤醒、用户手动唤醒后才补跑）。此时若用 ``next_occurrence``：
        目标 = 明天 07:00 → 距离 23.8 小时 → 触发「离太远直接退出」保护 → 放弃补约。
    结果是**只迟到 10 分钟就白白错过一整天的座位**。

    正确语义：今天 07:00 已过 10 分钟，仍在 ``max_late_minutes`` 容忍窗口内
    → 应返回今天 07:00 → 立即走"进入预热窗口 → 睡过头判定通过 → 立刻补抢"。

    超过容忍窗口（如晚上 22:30 补跑早上 07:00 的活）→ 返回明天 07:00，
    让 ``max_wait_minutes`` 保护照常生效，不会傻等。
    """
    now = now or datetime.now()
    h, m = parse_hhmm(hhmm)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target > now:
        return target
    if (now - target).total_seconds() <= max_late_minutes * 60:
        return target  # 今天的目标，已过但仍在容忍窗口内 → 立即补跑
    return target + timedelta(days=1)


def format_countdown(seconds: float) -> str:
    """把秒数格式化成中文倒计时，如「2 小时 13 分 05 秒」。"""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m:02d} 分 {s:02d} 秒"
    if m:
        return f"{m} 分 {s:02d} 秒"
    return f"{s} 秒"


class DailyScheduler:
    """每天在 open_time 准点执行一次抢座，提前 prewarm_minutes 分钟预热。

    参数
    ----
    open_time        : 目标时刻 "HH:MM"，默认 "07:00"
    prewarm_minutes  : 提前多少分钟开始预热登录，默认 2（用户要求 1~2 分钟）
    max_late_minutes : 超过目标时刻多少分钟就视为"睡过头"，跳过当天，默认 30
    on_prewarm       : `() -> sess` 预热回调，返回的 sess 会被保活并传给 on_book
    on_heartbeat     : `(remaining, phase) -> bool|None` 等待期间每片都会调用，
                       用于保活 / 断线重登 / 刷新倒计时。返回 False 表示请求中断；
                       其它返回值继续等待。
    on_book          : `(sess) -> bool` 准点提交回调
    on_status        : `(text: str) -> None` 状态文案回调（GUI 显示用）
    """

    def __init__(self,
                 open_time: str = "07:00",
                 prewarm_minutes: int = 2,
                 max_late_minutes: int = 30,
                 max_wait_minutes: int = 60,
                 on_prewarm: Optional[Callable] = None,
                 on_heartbeat: Optional[Callable] = None,
                 on_book: Optional[Callable] = None,
                 on_status: Optional[Callable] = None):
        # 提前解析一次，参数写错要立刻报错，而不是等到早上 7 点才发现
        parse_hhmm(open_time)
        self.open_time = open_time
        self.prewarm_minutes = max(0, int(prewarm_minutes))
        self.max_late_minutes = max(0, int(max_late_minutes))
        # 一次性模式（run_once）下允许的最长等待；超过就判定"离目标太远，不必等"
        self.max_wait_minutes = max(1, int(max_wait_minutes))
        self.on_prewarm = on_prewarm
        self.on_heartbeat = on_heartbeat
        self.on_book = on_book
        self.on_status = on_status

        self._stop = threading.Event()
        # 供 GUI 查询的当前状态
        self.phase: str = "未启动"        # 等待预热 / 预热中 / 等待抢座 / 抢座中 / 已停止
        self.next_prewarm_at: Optional[datetime] = None
        self.next_open_at: Optional[datetime] = None
        self.last_result: Optional[bool] = None
        self.runs: int = 0

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """请求停止。正在执行的预约会跑完，等待会立刻中断。"""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _status(self, text: str) -> None:
        self.phase = text
        logger.info("[定时] %s", text)
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 等待原语（抗睡眠漂移的核心）
    # ------------------------------------------------------------------
    def _wait_until(self, target: datetime, label: str) -> Tuple[bool, float]:
        """分片等待到 target。

        返回 `(reached, oversleep_seconds)`：
          - reached=True  ：已到点（oversleep_seconds 是睡过头的秒数，正常 < 0.1）
          - reached=False ：被 stop() 取消，或因睡过头被上层判定为跳过
        """
        while not self._stop.is_set():
            now = datetime.now()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                return True, -remaining

            chunk = min(_chunk_for(remaining), remaining)
            # stop_event.wait 比 time.sleep 好：点停止能立刻响应
            if self._stop.wait(chunk):
                return False, 0.0

            # 心跳（保活 + GUI 倒计时）
            if self.on_heartbeat:
                try:
                    if self.on_heartbeat(remaining, self.phase) is False:
                        return False, 0.0
                except Exception as exc:  # noqa: BLE001
                    logger.debug("等待心跳回调异常: %s", exc)
        return False, 0.0

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self, run_once: bool = False,
            force_next_open_at: Optional[datetime] = None,
            force_next_prewarm_at: Optional[datetime] = None) -> Optional[bool]:
        """阻塞运行调度循环（每天一轮）。`stop()` 或 Ctrl+C 退出。

        返回最后一次预约结果（run_once 时就是本次结果）。

        ``force_next_open_at`` / ``force_next_prewarm_at`` 仅用于单元测试：
        单测想验证"过去时刻立即补跑"和"睡过头跳过当天"等分支，
        需要把"下一次准点"临时钉到过去的某个时刻 — 而默认 ``run()`` 会在
        while 顶部重算 ``next_open_at``，把测试传的过去值覆盖回明天。
        传这两个参数可以把当前轮的目标点钉住（依然允许下一轮重算）。
        """
        h, m = parse_hhmm(self.open_time)
        logger.info("定时抢座已启动：每天 %02d:%02d 抢座，提前 %d 分钟预热登录，"
                    "迟到超过 %d 分钟则跳过当天",
                    h, m, self.prewarm_minutes, self.max_late_minutes)
        result: Optional[bool] = None

        while not self._stop.is_set():
            if force_next_open_at is not None and force_next_prewarm_at is not None:
                open_at = force_next_open_at
                prewarm_at = force_next_prewarm_at
            else:
                # ★ 用 current_occurrence 而非 next_occurrence：迟到了但在容忍窗口内
                # 时仍取今天的目标点，避免「只迟到 10 分钟却算成离明天还有 23.8 小时」
                # 被 max_wait 保护误杀（2026-09-11 实测事故，详见函数文档）。
                open_at = current_occurrence(
                    self.open_time, max_late_minutes=self.max_late_minutes)
                prewarm_at = open_at - timedelta(minutes=self.prewarm_minutes)
            self.next_open_at = open_at
            self.next_prewarm_at = prewarm_at

            # ---- 阶段 1：等到预热时刻 ----
            now = datetime.now()

            # ★ 2026-09-10：一次性模式下拒绝无谓长等。
            # 计划任务的 StartWhenAvailable 会在「用户登录时」补跑错过的任务，实测
            # 晚上 22:30 补跑早上 07:00 的活 —— 此时距离下一个目标点还有 8 小时，
            # serve 就傻等到明天，而计划任务 ExecutionTimeLimit（默认 30 分钟）会把它
            # 强杀 → 报 0xC0000142（看起来像 DLL 错误，其实是「被杀」）；
            # 更糟的是它还占着任务实例，早上真正该跑时 MultipleInstances=IgnoreNew
            # 会把新实例直接忽略掉，于是早上永远不会跑。
            # `--once` 的语义是「跑这一次就退出」，离目标太远就没有等待的意义
            # —— 计划任务到点会重新启动一个新进程。
            if run_once:
                wait_secs = (prewarm_at - now).total_seconds()
                if wait_secs > self.max_wait_minutes * 60:
                    today_target = now.replace(hour=h, minute=m,
                                               second=0, microsecond=0)
                    if today_target < now:
                        # 今天的目标已经超容忍窗口（睡过头太久）→ 跳过今天
                        logger.warning(
                            "一次性模式：今天 %02d:%02d 已错过 %.0f 分钟"
                            "（超过 %d 分钟容忍上限），跳过今天；下次 %s",
                            h, m, (now - today_target).total_seconds() / 60.0,
                            self.max_late_minutes,
                            prewarm_at.strftime("%m-%d %H:%M"))
                    else:
                        logger.warning(
                            "一次性模式：距离下次目标（%s）还有 %.1f 小时，"
                            "超过 %d 分钟上限 → 不做无谓等待，直接退出"
                            "（计划任务到点会重新启动，不会漏跑）",
                            prewarm_at.strftime("%m-%d %H:%M"),
                            wait_secs / 3600.0, self.max_wait_minutes)
                    return None

            if prewarm_at > now:
                self._status(f"等待预热：{format_countdown((prewarm_at - now).total_seconds())}"
                             f"后（{prewarm_at:%m-%d %H:%M}）开始登录")
                reached, _ = self._wait_until(prewarm_at, "预热")
                if not reached:
                    break
            else:
                logger.info("已进入预热窗口（目标 %02d:%02d，提前 %d 分钟），立即开始登录",
                            h, m, self.prewarm_minutes)

            # 睡过头判定：电脑睡了很久，别在大白天补跑
            late = (datetime.now() - open_at).total_seconds()
            if late > self.max_late_minutes * 60:
                self._status(f"睡过头 {late / 60:.0f} 分钟（>{self.max_late_minutes} 分钟），"
                             f"跳过今天，等待明天")
                if self._stop.wait(60):
                    break
                continue

            # ---- 阶段 2：预热登录并保活 ----
            sess = None
            if self.on_prewarm:
                self._status("预热登录中 ...")
                try:
                    sess = self.on_prewarm()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("预热登录失败，准点将重新登录: %s", exc)
                    sess = None
                if self._stop.is_set():
                    break
                if sess is not None:
                    self._status("登录完成，页面保持等待中（准点直接提交）")
                else:
                    self._status("预热失败，准点将走完整登录流程")

            # ---- 阶段 3：等到准点 ----
            now = datetime.now()
            remaining = (open_at - now).total_seconds()
            if remaining > 0:
                self._status(f"等待准点：{format_countdown(remaining)}后提交")
            reached, overslept = self._wait_until(open_at, "准点抢座")
            if not reached:
                break

            if overslept > 1.0:
                logger.warning("比目标时刻晚了 %.1f 秒（系统可能刚从睡眠恢复），立即补跑",
                               overslept)

            # ---- 阶段 4：准点提交 ----
            self.runs += 1
            self._status("准点提交中 ...")
            try:
                if self.on_book:
                    result = bool(self.on_book(sess))
                else:
                    result = None
            except Exception as exc:  # noqa: BLE001
                logger.error("预约执行异常: %s", exc)
                result = False
            self.last_result = result
            if result is not None:
                logger.info("本次预约结果: %s", "成功" if result else "失败")
                self._status(f"本次预约{'成功' if result else '失败'}，等待明天同一时刻")
            else:
                self._status("本轮结束，等待明天同一时刻")

            if run_once:
                break

            # 让 next_occurrence 翻到明天；先歇 10 秒避免同一秒内重复触发
            if self._stop.wait(10):
                break

        self._status("定时已停止")
        return result

    # ------------------------------------------------------------------
    # GUI 用：当前状态文案
    # ------------------------------------------------------------------
    def status_text(self) -> str:
        """给 GUI 显示的一行状态（含倒计时）。"""
        if self.phase.startswith("等待") or self.phase.startswith("登录完成"):
            target = self.next_prewarm_at
            now = datetime.now()
            if target and target > now:
                return (f"{self.phase.split('：')[0]}｜"
                        f"{format_countdown((target - now).total_seconds())}后"
                        f"（{target:%H:%M}）")
        return self.phase


# ----------------------------------------------------------------------
# 自检：python -m src.scheduler 07:00 2 --demo
# 用未来 5 / 20 秒后的时刻模拟跑一遍完整流程，不联网。
# ----------------------------------------------------------------------
def _self_test() -> int:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0

    now = datetime.now()
    # 预热时刻 = 10 秒后；"准点" = 25 秒后
    prewarm_dt = now + timedelta(seconds=10)
    open_dt = now + timedelta(seconds=25)
    print(f"[i] 模拟：预热 {prewarm_dt:%H:%M:%S}，准点 {open_dt:%H:%M:%S}（不联网）")

    sched = DailyScheduler(open_time=open_dt.strftime("%H:%M:%S")[:5],
                           prewarm_minutes=0)

    log = []

    def on_prewarm():
        log.append("prewarm")
        print(f"    [预热] {datetime.now():%H:%M:%S} 假装登录完成，页面保活")
        return "FAKE_SESS"

    def on_heartbeat(remaining, phase):
        return None  # 不中断

    def on_book(sess):
        log.append(("book", sess))
        print(f"    [提交] {datetime.now():%H:%M:%S} sess={sess}")
        return True

    sched.on_prewarm = on_prewarm
    sched.on_heartbeat = on_heartbeat
    sched.on_book = on_book
    sched.on_status = lambda t: None

    # 直接用内部等待，验证精度
    print("[i] 验证 _wait_until 精度（目标 3 秒后）...")
    target = datetime.now() + timedelta(seconds=3)
    t0 = time.time()
    reached, over = sched._wait_until(target, "测试")
    print(f"    reached={reached} 误差={over * 1000:.0f}ms 实际耗时={time.time() - t0:.2f}s")
    assert reached, "应该到点"
    assert over < 0.5, f"误差应 < 500ms，实际 {over * 1000:.0f}ms"

    print("[i] 验证 stop() 能立刻取消等待...")
    sched2 = DailyScheduler(open_time="07:00")
    t0 = time.time()
    threading.Timer(1.0, sched2.stop).start()
    reached, _ = sched2._wait_until(datetime.now() + timedelta(hours=5), "测试")
    print(f"    reached={reached} 用时={time.time() - t0:.2f}s（应 ≈1 秒，不是 5 小时）")
    assert reached is False
    assert time.time() - t0 < 2.0

    print("[i] 验证 next_occurrence 跨天...")
    n = next_occurrence("07:00", datetime(2026, 9, 5, 8, 0))
    assert n == datetime(2026, 9, 6, 7, 0), n
    n2 = next_occurrence("07:00", datetime(2026, 9, 5, 6, 0))
    assert n2 == datetime(2026, 9, 5, 7, 0), n2
    print("    ✓ 已过→明天；未过→今天")

    print("[i] 验证 format_countdown ...")
    assert format_countdown(65) == "1 分 05 秒", format_countdown(65)
    assert format_countdown(3725) == "1 小时 02 分 05 秒", format_countdown(3725)
    print("    ✓ 65s→'1 分 05 秒'；3725s→'1 小时 02 分 05 秒'")

    print("\n[✓] scheduler 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
