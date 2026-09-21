"""验证「迟到补跑」修复（2026-09-11 实测事故回归测试）。

事故现场
--------
计划任务 06:58 没唤醒电脑，用户 07:10 手动唤醒后才补跑。程序一启动就用
``next_occurrence`` 算出「下次目标 = 明天 07:00，还有 23.8 小时」→ 触发
``max_wait_minutes`` 保护 → 直接退出，**只迟到 10 分钟就丢掉一整天**。

修复
----
新增 ``current_occurrence(hhmm, now, max_late_minutes)``：今天的目标已过、
但仍在 ``max_late_minutes`` 容忍窗口内时，返回**今天**的目标点，让流程立即补跑。

本测试用 FixedDatetime 冻结 now，端到端跑 ``run(run_once=True)``，
断言 on_book 真的被调用（或按预期退出）。
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.scheduler as S  # noqa: E402
from src.scheduler import DailyScheduler, current_occurrence  # noqa: E402


class FixedDatetime(datetime):
    """datetime 子类，只覆盖 now()，保留全部运算行为。"""
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def freeze(now: datetime):
    FixedDatetime._fixed = now
    S.datetime = FixedDatetime


def unfreeze():
    S.datetime = datetime


# --------------------------------------------------------------------------
# 1. 纯函数：current_occurrence 的窗口判定
# --------------------------------------------------------------------------
def test_current_occurrence_window():
    base = datetime(2026, 9, 11)
    cases = [
        (base.replace(hour=6, minute=0), "today", "未到点 → 今天 07:00（等待）"),
        (base.replace(hour=7, minute=0), "today", "准点 → 今天 07:00"),
        (base.replace(hour=7, minute=10), "today", "迟到 10 分 → 今天（立即补跑）"),
        (base.replace(hour=7, minute=29), "today", "迟到 29 分 → 今天（立即补跑）"),
        (base.replace(hour=7, minute=31), "tomorrow", "迟到 31 分 → 明天（跳过今天）"),
        (base.replace(hour=22, minute=30), "tomorrow", "晚上补跑 → 明天（不傻等）"),
    ]
    for now, want, label in cases:
        got = current_occurrence("07:00", now, max_late_minutes=30)
        got_day = "today" if got.date() == now.date() else "tomorrow"
        want_dt = now if want == "today" else now
        assert got_day == want, f"{label}: 期望 {want}，实得 {got_day}"
        if want == "today":
            assert got.hour == 7 and got.minute == 0, f"{label}: 应钉在今天 07:00"
        print(f"  ✓ {label}")


def test_current_occurrence_never_earlier_than_window():
    """max_late=0 时，迟到 1 秒就应翻到明天（边界安全）。"""
    now = datetime(2026, 9, 11, 7, 0, 1)
    got = current_occurrence("07:00", now, max_late_minutes=0)
    assert got.date() != now.date(), "max_late=0 时迟到 1 秒应翻明天"
    print("  ✓ max_late=0 边界：迟到 1 秒 → 明天")


# --------------------------------------------------------------------------
# 2. 端到端：07:10 补跑必须真的执行 on_book
# --------------------------------------------------------------------------
def _run_once_at(now: datetime, *, open_time="07:00", max_late=30, max_wait=60):
    """在冻结时刻跑一轮 run_once，返回 (result, book_called, prewarm_called)。"""
    calls = {"book": 0, "prewarm": 0}

    def on_prewarm():
        calls["prewarm"] += 1
        return object()  # 假装拿到 sess

    def on_book(sess):
        calls["book"] += 1
        return True

    freeze(now)
    try:
        sched = DailyScheduler(
            open_time=open_time,
            prewarm_minutes=2,
            max_late_minutes=max_late,
            max_wait_minutes=max_wait,
            on_prewarm=on_prewarm,
            on_book=on_book,
        )
        result = sched.run(run_once=True)
    finally:
        unfreeze()
    return result, calls["book"], calls["prewarm"]


def test_late_10_minutes_still_books():
    """★ 事故场景：07:10 补跑必须执行（修复前这里是直接退出）。"""
    result, book, prewarm = _run_once_at(datetime(2026, 9, 11, 7, 10, 0))
    assert book == 1, f"07:10 补跑应执行 on_book 1 次，实得 {book}"
    assert result is True, f"结果应为 True，实得 {result}"
    print("  ✓ 07:10 补跑：on_book 已执行，result=True（修复生效）")


def test_late_25_minutes_still_books():
    """迟到 25 分（仍在 30 分容忍内）也要跑。"""
    result, book, _ = _run_once_at(datetime(2026, 9, 11, 7, 25, 0))
    assert book == 1, f"07:25 应执行 on_book，实得 {book}"
    print("  ✓ 07:25 补跑：on_book 已执行")


def test_overslept_beyond_window_skips():
    """迟到 45 分（超过 30 分容忍）→ 不执行，跳过今天。"""
    result, book, _ = _run_once_at(datetime(2026, 9, 11, 7, 45, 0))
    assert book == 0, f"07:45 超过容忍窗口，不应执行 on_book，实得 {book}"
    assert result is None, f"应返回 None（本轮未执行），实得 {result}"
    print("  ✓ 07:45 睡过头：未执行，跳过今天")


def test_evening_catchup_still_declines():
    """晚上 22:30 补跑早上的活 → 仍应拒绝傻等（max_wait 保护不能被破坏）。"""
    result, book, _ = _run_once_at(datetime(2026, 9, 11, 22, 30, 0))
    assert book == 0, f"22:30 补跑不应执行，实得 {book}"
    assert result is None, f"应返回 None，实得 {result}"
    print("  ✓ 22:30 补跑：照旧拒绝无谓等待（保护未失效）")


def test_on_time_normal_path_unaffected():
    """准点整点启动 → 正常执行，行为不变。

    注意：不能用「06:59 启动后在 07:00 提交」来测 —— now 被冻结在 06:59，
    ``_wait_until(07:00)`` 会永远差 60 秒，测试自己死循环（不是代码问题）。
    这里直接用 07:00:00 整：remaining=0，``_wait_until`` 立即返回。
    """
    result, book, prewarm = _run_once_at(datetime(2026, 9, 11, 7, 0, 0))
    assert book == 1, f"07:00 应执行 on_book，实得 {book}"
    assert prewarm == 1, f"应先预热，实得 {prewarm}"
    print("  ✓ 07:00 准点启动：预热 + 提交路径不受影响")


if __name__ == "__main__":
    print("=" * 62)
    print("迟到补跑修复验证（2026-09-11 事故回归）")
    print("=" * 62)
    print("\n[1] 纯函数 current_occurrence 窗口判定")
    test_current_occurrence_window()
    test_current_occurrence_never_earlier_than_window()

    print("\n[2] 端到端 run_once 行为")
    test_late_10_minutes_still_books()
    test_late_25_minutes_still_books()
    test_overslept_beyond_window_skips()
    test_evening_catchup_still_declines()
    test_on_time_normal_path_unaffected()

    print("\n" + "=" * 62)
    print("全部断言通过 ✓")
    print("=" * 62)
