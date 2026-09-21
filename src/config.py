"""配置加载模块：读取 config.json，提供带默认值的配置访问。"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "username": "",
    "password": "",
    "campus": "主校区",
    "room": "第一阅览室",
    "seat": "001",             # 座位号/label；济大阅览室通常用 001, 125 等三位数 label
    "date": "tomorrow",
    "start": "08:00",
    "end": "12:00",
    "mode": "browser",         # browser | api | auto
    "headless": True,
    "browser_channel": "msedge",  # Playwright 浏览器通道：msedge / chrome / msedge-beta
    "max_retry": 5,
    "slide": {
        "slider_candidates": [
            ".slider-move-btn",
            ".slider-btn", ".slide-block", "#slider",
            ".nc_iconfont.btn_slide", ".yidun_slider", ".captcha-slider-btn",
        ],
        "bg_candidates": [
            "#tianai-captcha-slider-bg-img",
            ".slider-bg img", ".slide-bg-img", ".yidun_bgimg",
            ".captcha-bg-img", "img[src*='bg']",
        ],
        "piece_candidates": [
            ".slider-img-div img",
            ".slider-piece img", ".slide-block-img", ".yidun_jigsaw",
            ".captcha-piece-img", "img[src*='fg']",
        ],
        "tolerance": 0,  # 缺口检测已较准确，拖动到计算位置即可
    },
    "debug": False,
    "dry_run": False,  # True 时拦截 freeBook，用于离线测试滑块但不真实预约
    # 周二特例：图书馆每周二 12:00~16:00 闭馆，无法连约整天。
    # 当【预约目标日期】落在周二时，改用下面这组时间（默认只约上午 08:30~12:00），
    # 而不是 config 里的 start/end（如 08:20~22:00）。
    # 注意判断依据是目标日期本身（date=tomorrow 时即"明天是不是周二"）。
    "tuesday": {
        "enabled": True,
        "start": "08:30",
        "end": "12:00",
    },
    # 定时抢座：每天 open_time 准点提交，提前 prewarm_minutes 分钟预热登录并保活页面
    "schedule": {
        "enabled": False,        # GUI 启动时是否自动挂上定时守护
        "open_time": "07:00",    # 济大约 07:00 开放次日预约
        "prewarm_minutes": 2,    # 提前几分钟登录（SSO 约 8~10 秒，1~2 分钟足够）
        "keep_awake": True,      # 等待期间阻止系统睡眠（否则定时任务可能不触发）
        # 迟到超过这个分钟数视为睡过头，跳过当天（2026-09-07 放宽：30→90）。
        # 原 30 分钟太严：实测电脑 07:54 醒来（比 07:00 晚 54 分钟）直接被判睡过头跳过，
        # 但济大 07:00 开放后 1~2 小时内通常还有座位，晚一点补跑比完全放弃强。
        "max_late_minutes": 90,
    },
}


def resolve_target_date(expr: str | None) -> str:
    """把配置里的 today / tomorrow / YYYY-MM-DD 统一解析成 YYYY-MM-DD 字符串。"""
    expr = str(expr if expr is not None else "today").strip()
    today = date.today()
    if expr == "today":
        return today.strftime("%Y-%m-%d")
    if expr == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return expr


def weekday_of(md: str) -> int | None:
    """返回 YYYY-MM-DD 的星期（周一=0 … 周日=6）；无法解析时返回 None。"""
    try:
        return date.fromisoformat(str(md).strip()[:10]).weekday()
    except (ValueError, TypeError):
        return None


class Config:
    """封装配置字典，提供点号风格与默认值回退。"""

    def __init__(self, data: Dict[str, Any]):
        # 合并默认值，避免缺少字段时报错
        self._data = {**DEFAULTS, **data}
        self._data["slide"] = {**DEFAULTS["slide"], **(data.get("slide") or {})}
        self._data["schedule"] = {**DEFAULTS["schedule"], **(data.get("schedule") or {})}
        self._data["tuesday"] = {**DEFAULTS["tuesday"], **(data.get("tuesday") or {})}

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def username(self) -> str:
        return self._data["username"]

    @property
    def password(self) -> str:
        return self._data["password"]

    @property
    def mode(self) -> str:
        return self._data.get("mode", "auto")

    @property
    def slide(self) -> Dict[str, Any]:
        return self._data["slide"]

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    # ---------------------------------------------------------------- 预约时间窗
    def target_date(self, cfg_date: str | None = None) -> str:
        """本次预约的目标日期（YYYY-MM-DD）。不传则取配置里的 date。"""
        if cfg_date is None:
            cfg_date = self.get("date", "tomorrow")
        return resolve_target_date(cfg_date)

    def is_tuesday(self, cfg_date: str | None = None) -> bool:
        """预约目标日期是否落在周二（图书馆 12:00~16:00 闭馆）。"""
        return weekday_of(self.target_date(cfg_date)) == 1

    def time_window(self, cfg_date: str | None = None) -> tuple[str, str]:
        """返回本次预约实际生效的 (start, end)。

        默认用配置里的 start/end；若目标日期是周二且 tuesday.enabled 为真，
        则改用 tuesday.start/end（默认 08:30~12:00）—— 因为图书馆当天
        12:00~16:00 闭馆，按整天连约会提交失败。
        """
        start = self.get("start") or DEFAULTS["start"]
        end = self.get("end") or DEFAULTS["end"]
        rule = self._data.get("tuesday") or {}
        if rule.get("enabled", True) and self.is_tuesday(cfg_date):
            return rule.get("start") or start, rule.get("end") or end
        return start, end

    def time_window_note(self, cfg_date: str | None = None) -> str:
        """周二规则生效时返回一句中文说明（供日志/界面展示），否则返回空串。"""
        rule = self._data.get("tuesday") or {}
        if not rule.get("enabled", True) or not self.is_tuesday(cfg_date):
            return ""
        s, e = self.time_window(cfg_date)
        return (f"{self.target_date(cfg_date)} 是周二，图书馆 12:00~16:00 闭馆"
                f" → 本次改为预约 {s}~{e}")


# 项目根目录（config.json 与脚本同目录时，从 src 上一级读取）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(explicit: str | Path | None = None) -> Config:
    """按优先级加载配置：显式路径 > 工作目录 config.json > 项目根 config.json。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / "config.json")
    candidates.append(PROJECT_ROOT / "config.json")
    for c in candidates:
        if c.exists():
            return Config.load(c)
    raise FileNotFoundError(
        "未找到 config.json，请复制 config.example.json 为 config.json 并填写账号信息。"
    )
