"""周二闭馆特例回归测试（2026-09-20 新增）。

规则：图书馆每周二 12:00~16:00 闭馆，无法连约整天 →
当【预约目标日期】是周二时，自动把时间窗换成 08:30~12:00（可配置）。

覆盖点：
1. Config.time_window 的判定（周二/非周二/开关/自定义值/无法解析的日期）
2. Config.is_tuesday / time_window_note
3. _make_date 与 config.resolve_target_date 行为一致（单一事实源）
4. 【关键】铁证核对 _evidence_verified 必须用调整后的时间窗，否则永远查不到记录
5. 源码级防回归：booker 里不许再出现裸的 cfg.get("start")/("end") 读时间

运行：python debug/verify_tuesday.py
"""
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config, resolve_target_date, weekday_of  # noqa: E402

PASS = []


def ok(msg):
    PASS.append(msg)
    print("  ✓ " + msg)


def make_cfg(**overrides):
    data = {
        "username": "x", "password": "y",
        "room": "第七阅览室中区", "seat": "142",
        "start": "08:20", "end": "22:00",
        "date": "tomorrow", "dry_run": False,
    }
    data.update(overrides)
    return Config(data)


def _weekday_of_iso(md):
    return date.fromisoformat(md).weekday()


def test_weekday_helper():
    """weekday_of：周一=0 … 周日=6；非法输入返回 None。"""
    assert weekday_of("2026-09-21") == 0, "2026-09-21 应为周一"
    assert weekday_of("2026-09-22") == 1, "2026-09-22 应为周二"
    assert weekday_of("2026-09-23") == 2, "2026-09-23 应为周三"
    assert weekday_of("not-a-date") is None
    assert weekday_of("") is None
    ok("weekday_of 周一=0/周二=1/周三=2，非法输入返回 None")


def test_date_resolver_dynamic():
    """resolve_target_date 动态解析 today/tomorrow（禁止硬编码日期）。"""
    today = date.today()
    assert resolve_target_date("today") == today.strftime("%Y-%m-%d")
    assert resolve_target_date("tomorrow") == (today + timedelta(days=1)).strftime("%Y-%m-%d")
    assert resolve_target_date("2026-09-22") == "2026-09-22"
    ok("resolve_target_date 动态解析 today/tomorrow + 透传显式日期")


def test_make_date_delegates_to_config():
    """_make_date 与 config.resolve_target_date 必须完全一致（单一事实源）。"""
    from src.booker import _make_date
    for expr in ("today", "tomorrow", "2026-09-22"):
        assert _make_date(expr) == resolve_target_date(expr), f"{expr} 解析结果不一致"
    ok("_make_date 委托给 config.resolve_target_date，两处结果一致")


def test_tuesday_uses_morning_window():
    """目标日期是周二 → 08:30~12:00。"""
    cfg = make_cfg(date="2026-09-22")
    assert cfg.is_tuesday() is True
    assert cfg.time_window() == ("08:30", "12:00"), cfg.time_window()
    ok("周二(2026-09-22) → 时间窗 08:30~12:00")


def test_non_tuesday_uses_default_window():
    """非周二 → 用 config 里的 start/end。"""
    for md, label in (("2026-09-21", "周一"), ("2026-09-23", "周三"),
                      ("2026-09-20", "周日")):
        cfg = make_cfg(date=md)
        assert cfg.is_tuesday() is False, f"{label} 不该判为周二"
        assert cfg.time_window() == ("08:20", "22:00"), f"{label} 时间窗被误改"
    ok("周一/周三/周日 → 保持 08:20~22:00")


def test_tomorrow_dynamic_switch():
    """date=tomorrow 时按【明天】判断：明天是周二才调整。"""
    tomorrow = date.today() + timedelta(days=1)
    cfg = make_cfg(date="tomorrow")
    expect = ("08:30", "12:00") if tomorrow.weekday() == 1 else ("08:20", "22:00")
    assert cfg.time_window() == expect, f"明天={tomorrow} 预期 {expect}，实得 {cfg.time_window()}"
    ok(f"date=tomorrow 按明天判定（明天={tomorrow} 周{_weekday_of_iso(tomorrow.isoformat())+1}）")


def test_disable_switch():
    """tuesday.enabled=False → 即使周二也用默认时间窗（可一键关掉）。"""
    cfg = make_cfg(date="2026-09-22", tuesday={"enabled": False})
    assert cfg.is_tuesday() is True
    assert cfg.time_window() == ("08:20", "22:00")
    assert cfg.time_window_note() == ""
    ok("tuesday.enabled=False → 周二仍用 08:20~22:00（可关闭）")


def test_custom_tuesday_values():
    """tuesday.start/end 可自定义，且只覆盖提供的字段。"""
    cfg = make_cfg(date="2026-09-22", tuesday={"start": "09:00"})
    assert cfg.time_window() == ("09:00", "12:00"), cfg.time_window()
    cfg2 = make_cfg(date="2026-09-22", tuesday={"enabled": True, "start": "08:00", "end": "11:30"})
    assert cfg2.time_window() == ("08:00", "11:30")
    ok("tuesday 自定义 start/end 生效，缺省字段回退默认")


def test_default_enabled():
    """不写 tuesday 段时默认开启（老 config.json 无需改动也能生效）。"""
    cfg = Config({"username": "x", "password": "y", "date": "2026-09-22",
                  "start": "08:20", "end": "22:00"})
    assert cfg.time_window() == ("08:30", "12:00")
    ok("config 无 tuesday 段时默认开启规则（向后兼容）")


def test_invalid_date_falls_back():
    """日期无法解析时不得抛异常，退回默认时间窗。"""
    cfg = make_cfg(date="下周三")
    assert cfg.is_tuesday() is False
    assert cfg.time_window() == ("08:20", "22:00")
    assert cfg.time_window_note() == ""
    ok("非法日期 → 不抛异常，退回默认时间窗")


def test_note_text():
    """time_window_note 周二有说明、非周二为空。"""
    assert "周二" in make_cfg(date="2026-09-22").time_window_note()
    assert make_cfg(date="2026-09-21").time_window_note() == ""
    ok("time_window_note：周二返回中文说明，其余为空")


def test_evidence_uses_adjusted_window():
    """【关键】铁证核对必须用调整后的时间窗，否则周二永远查不到记录。"""
    import src.booker as booker
    seen = {}

    def fake_reservation_exists(page, make_date, room, start_mm, end_mm,
                                retries=1, retry_delay_ms=1200):
        seen.update(date=make_date, room=room, start=start_mm, end=end_mm)
        return True

    orig = booker._reservation_exists
    booker._reservation_exists = fake_reservation_exists
    try:
        cfg = make_cfg(date="2026-09-22", room="第七阅览室中区")
        assert booker._evidence_verified(object(), cfg, "vm.orderSuccess") is True
    finally:
        booker._reservation_exists = orig

    assert seen["start"] == "08:30", f"铁证应核对 08:30，实得 {seen['start']}"
    assert seen["end"] == "12:00", f"铁证应核对 12:00，实得 {seen['end']}"
    assert seen["date"] == "2026-09-22"
    ok("铁证核对使用调整后的 08:30~12:00（而非配置的 08:20~22:00）")


def test_evidence_uses_default_on_non_tuesday():
    """非周二时铁证核对仍走默认时间窗。"""
    import src.booker as booker
    seen = {}

    def fake(page, make_date, room, start_mm, end_mm, retries=1, retry_delay_ms=1200):
        seen.update(start=start_mm, end=end_mm)
        return True

    orig = booker._reservation_exists
    booker._reservation_exists = fake
    try:
        booker._evidence_verified(object(), make_cfg(date="2026-09-21"), "t")
    finally:
        booker._reservation_exists = orig
    assert (seen["start"], seen["end"]) == ("08:20", "22:00"), seen
    ok("非周二铁证核对走 08:20~22:00")


def test_no_raw_start_end_reads_in_booker():
    """源码级防回归：booker.py 不应再有裸读 cfg.get("start"/"end") 当时间用。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "booker.py"), encoding="utf-8").read()
    bad = re.findall(r'cfg\.get\(\s*["\'](?:start|end)["\']', src)
    assert not bad, f"仍有 {len(bad)} 处裸读 start/end，应改用 cfg.time_window()"
    assert "cfg.time_window()" in src, "booker.py 未使用 cfg.time_window()"
    ok("booker.py 已无裸读 cfg.get('start'/'end')，统一走 time_window()")


def test_gui_preserves_tuesday_on_save():
    """GUI 保存配置时必须保留 tuesday 段，不能被抹掉。"""
    gui = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "gui.py"), encoding="utf-8").read()
    assert 'data["tuesday"]' in gui, "save_config 未回写 tuesday，保存一次就会丢失规则"
    ok("GUI save_config 会保留 tuesday 段")


def main():
    print("=" * 62)
    print("周二闭馆特例 — 回归测试")
    print("=" * 62)
    tests = [
        test_weekday_helper,
        test_date_resolver_dynamic,
        test_make_date_delegates_to_config,
        test_tuesday_uses_morning_window,
        test_non_tuesday_uses_default_window,
        test_tomorrow_dynamic_switch,
        test_disable_switch,
        test_custom_tuesday_values,
        test_default_enabled,
        test_invalid_date_falls_back,
        test_note_text,
        test_evidence_uses_adjusted_window,
        test_evidence_uses_default_on_non_tuesday,
        test_no_raw_start_end_reads_in_booker,
        test_gui_preserves_tuesday_on_save,
    ]
    for t in tests:
        t()
    print("-" * 62)
    print(f"全部通过：{len(PASS)} 项断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
