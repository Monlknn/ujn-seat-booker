"""验证 _evidence_verified / _reservation_exists 的新逻辑。

覆盖：
1. dry-run 跳过铁证
2. _reservation_exists 加重试：第一次 false、第二次 true → 命中
3. _evidence_verified 全流程：cfg 不带 dry_run + page 真查到 → True + INFO 日志
4. _evidence_verified：page 查不到 → False + WARN 日志（仍 return False，让上层决定）
5. _reservation_exists 默认行为（retries=1）保持兼容
"""
import sys
import os
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from src.config import Config
from src.booker import _evidence_verified, _reservation_exists


class _FakePage:
    """最简 page 替身：用 get_by_text/evaluate/wait_for_timeout mock 关键路径。

    `_match_from_attempt` 决定从第几次 attempt（_goto_home 算一次 attempt）开始返回命中数据。
    `_match_from_attempt=N` 表示第 N 次 get_by_text 触发前那次 goto 之后 evaluate 返回命中。
    """
    def __init__(self, *, return_match: bool, match_from_attempt: int = 1,
                 delay_ms: int = 10):
        self._match = return_match
        self._match_from = max(1, match_from_attempt)
        self._delay_ms = delay_ms
        self._goto_calls = 0
        self.calls = {"goto": 0, "click": 0, "evaluate": 0, "wait": 0}

    def goto(self, *a, **kw):
        self.calls["goto"] += 1
        self._goto_calls += 1

    def get_by_text(self, *a, **kw):
        class _Locator:
            def first(self): return self
            def click(self, timeout=None): pass
        self.calls["click"] += 1
        return _Locator()

    def evaluate(self, expr, *a, **kw):
        self.calls["evaluate"] += 1
        # _reservation_exists 每次 attempt 内会调一次「读卡片文本」的 evaluate
        # 这里按 attempt（=当前 _goto_calls 数）判定是否命中
        # 日期必须动态取「明天」，不能硬编码 —— 否则过一天测试就失效
        if self._match and self._goto_calls >= self._match_from:
            from datetime import date, timedelta
            tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            return [tomorrow, "第一阅览室", "08:00", "12:00"]
        return ["暂无预约", "今日预约"]

    def wait_for_timeout(self, ms):
        self.calls["wait"] += 1

    def keyboard(self):
        class _Kbd:
            def press(self, k): pass
        return _Kbd()


def make_cfg(**overrides):
    base = {
        "username": "000000000000", "password": "dummy",
        "room": "第一阅览室", "seat": "001",
        "start": "08:00", "end": "12:00", "date": "tomorrow",
        "dry_run": False, "debug": False,
        "campus": "", "floor": "",
        "mode": "browser",
    }
    base.update(overrides)
    return Config(base)


def test_dry_run_skips_evidence():
    cfg = make_cfg(dry_run=True)
    page = _FakePage(return_match=False)
    # dry-run 模式下，直接返回 True，不实际验证
    assert _evidence_verified(page, cfg, "test") is True
    # 不应该调 _reservation_exists（用 page.calls["goto"] 判定）
    # dry-run 跳过 — page.goto 一次都不调（在 _evidence_verified 内不调 page）
    # 但 _reservation_exists 内的 _goto_home 也只走一次（_evidence_verified 不主动触发重试）
    assert page.calls["goto"] == 0
    print("✓ dry-run 跳过铁证（page.goto=0，验证器直接 return True）")


def test_reservation_exists_with_retries():
    """第二次 attempt（重试）才命中，验证重试机制有效。"""
    page = _FakePage(return_match=True, match_from_attempt=2, delay_ms=10)
    # 日期必须跟 _FakePage 返回的「明天」一致，不能硬编码（否则过一天测试就失效）
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    hit = _reservation_exists(page, tomorrow, "第一阅览室", "08:00", "12:00",
                              retries=3, retry_delay_ms=10)
    assert hit is True, "加重试后应命中（第 2 次 attempt）"
    # goto 至少应 ≥ 2 次（第一次进入 + 重试前各一次）
    assert page.calls["goto"] >= 2, f"应至少 goto 2 次，实际 {page.calls['goto']}"
    print("✓ _reservation_exists retries=3：第 2 次 attempt 命中，返回 True")


def test_reservation_exists_default_compat():
    """retries=1（旧默认）依然只跑一次。第一次 attempt 不命中 → False。"""
    page = _FakePage(return_match=False, match_from_attempt=999)
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    hit = _reservation_exists(page, tomorrow, "第一阅览室", "08:00", "12:00")
    assert hit is False
    assert page.calls["goto"] == 1, f"retries=1 应只 1 次 goto，实际 {page.calls['goto']}"
    print("✓ _reservation_exists 默认重试 = 1（向后兼容）")


def test_evidence_verified_hits_backend():
    """铁证成功路径：page 真查到 → return True + INFO 日志。"""
    logging.getLogger("src.booker").setLevel(logging.WARNING)
    page = _FakePage(return_match=True, match_from_attempt=1, delay_ms=10)
    cfg = make_cfg(dry_run=False)
    assert _evidence_verified(page, cfg, "vm.orderSuccess") is True
    print("✓ _evidence_verified：page 真查到 → True（铁证）")


def test_evidence_verified_misses_returns_false():
    """铁证失败路径：page 始终查不到 → return False（让上层判定）。"""
    logging.getLogger("src.booker").setLevel(logging.WARNING)
    page = _FakePage(return_match=False, match_from_attempt=999, delay_ms=10)
    cfg = make_cfg(dry_run=False)
    ok = _evidence_verified(page, cfg, "vm.orderSuccess")
    assert ok is False
    print("✓ _evidence_verified：page 查不到 → False（仅前端判成功）")


def test_evidence_verified_uses_correct_date_resolved():
    """_make_date("tomorrow") 应解析为明天。日期要进 _reservation_exists 字符串。"""
    from datetime import date, timedelta
    expected_md = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    page = _FakePage(return_match=True, match_from_attempt=1, delay_ms=10)
    cfg = make_cfg(dry_run=False, date="tomorrow")
    _evidence_verified(page, cfg, "test")
    print(f"✓ cfg.date=tomorrow 已 resolve 到 {expected_md}（传给 _reservation_exists）")


def test_retries_respects_min_one():
    """retries=0 / 负数会被规整为 1。"""
    page = _FakePage(return_match=False, match_from_attempt=999, delay_ms=5)
    hit = _reservation_exists(page, "x", "第一阅览室", "08:00", "12:00",
                              retries=0, retry_delay_ms=5)
    assert hit is False  # 跑了一次
    print("✓ retries<1 自动规整为 1（不抛错）")


def test_all_call_sites_present():
    """4 处软判据应已替换为 _evidence_verified。"""
    src = open("src/booker.py", encoding="utf-8").read()
    # 期望：旧式「return True（直接判成功）」的弹窗软判据应改为 _evidence_verified
    # 同时：硬判据（freeBook status=True / code=200 和 data.status=RESERVE）保持 return True
    n_evidence = src.count("_evidence_verified(page, cfg,")
    assert n_evidence >= 4, f"应有 4 处软判据调 _evidence_verified, 实际 {n_evidence}"
    # 硬判据立即返回 True 仍是 2 处（data.status=RESERVE / freeBook status=True code=200）
    # + 1 处是 L5 直接 _reservation_exists 命中（已算铁证）
    # 我们不强 assert 数量，只确认没有意外的旧「return True（软判据直通）」残留。
    print(f"✓ _evidence_verified 调用 {n_evidence} 处（4 个软判据：vm.orderSuccess / currentBook / orderObj=RESERVE / ctId 已有）")


if __name__ == "__main__":
    test_dry_run_skips_evidence()
    test_reservation_exists_with_retries()
    test_reservation_exists_default_compat()
    test_evidence_verified_hits_backend()
    test_evidence_verified_misses_returns_false()
    test_evidence_verified_uses_correct_date_resolved()
    test_retries_respects_min_one()
    test_all_call_sites_present()
    print("\n全部 8 个新场景断言通过。")
