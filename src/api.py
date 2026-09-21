"""REST API 直连模块。

济南大学座位系统提供了一套未鉴权的 REST 接口（多个开源项目长期验证可用）：
  - GET  /rest/auth?username=&password=             -> 返回 token（登录无需验证码）
  - GET  /rest/v2/rooms?token=                       -> 阅览室列表
  - GET  /rest/v2/room/layoutByDate/{roomId}/{date}?token= -> 座位布局（含 seat id）
  - POST /rest/v2/freeBook                          -> 预约（token/startTime/endTime/seat/date）

相比辽大项目用 Selenium 爬网页，这里用 requests 直接打接口，更快更稳定。
若接口后续被滑块/签名强制校验，则在 booker 层降级到浏览器模式。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .logger import logger

BASE = "http://seat.ujn.edu.cn"
HTTP_TIMEOUT = 15


def _requests():
    """按需导入 requests。

    浏览器模式（以及 GUI 启动）并不需要 requests，若设为顶层 import，
    缺这个包会导致连界面都打不开。这里延迟到真正发请求时再导入。
    """
    try:
        import requests
        return requests
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 requests（仅 api/auto 模式需要）。请安装: pip install requests"
        ) from exc


def _http_get(path: str, params: Optional[Dict] = None) -> Dict:
    url = BASE + path
    try:
        r = _requests().get(url, params=params, timeout=HTTP_TIMEOUT)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("GET %s 失败: %s", url, exc)
        return {"status": "fail", "message": str(exc), "code": "-1", "data": None}


def _http_post(path: str, data: Dict) -> Dict:
    url = BASE + path
    try:
        r = _requests().post(url, data=data, timeout=HTTP_TIMEOUT)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("POST %s 失败: %s", url, exc)
        return {"status": "fail", "message": str(exc), "code": "-1", "data": None}


def get_token(username: str, password: str) -> Optional[str]:
    """登录拿 token。返回 token 字符串或 None。"""
    resp = _http_get("/rest/auth", {"username": username, "password": password})
    if resp.get("status") == "success" and resp.get("data", {}).get("token"):
        token = resp["data"]["token"]
        logger.info("登录成功，token=%s", token)
        return token
    logger.warning("登录失败: %s", resp.get("message", resp))
    return None


def get_rooms(token: str) -> List[Dict]:
    """获取阅览室列表（含 roomId / room / floor）。"""
    resp = _http_get("/rest/v2/rooms", {"token": token})
    if resp.get("status") == "success":
        return resp.get("data", [])
    logger.warning("获取阅览室失败: %s", resp.get("message", resp))
    return []


def get_room_id(token: str, room_name: str) -> Optional[int]:
    """按阅览室名模糊匹配返回 roomId。"""
    rooms = get_rooms(token)
    for r in rooms:
        if room_name in r.get("room", ""):
            return r["roomId"]
    logger.warning("未找到阅览室: %s（可选项: %s）",
                   room_name, [r.get("room") for r in rooms])
    return None


def get_seat_id(token: str, room_id: int, seat_no: str, date: str) -> Optional[int]:
    """根据阅览室 id、座位号（如 '001'）、日期返回座位唯一 id。"""
    resp = _http_get(f"/rest/v2/room/layoutByDate/{room_id}/{date}",
                     {"token": token})
    if resp.get("status") != "success":
        logger.warning("获取座位布局失败: %s", resp.get("message", resp))
        return None
    layout = resp.get("data", {}).get("layout", {})
    for key, cell in layout.items():
        if cell.get("type") == "seat" and str(cell.get("name", "")).zfill(3) == str(seat_no).zfill(3):
            return cell["id"]
    # 部分接口 name 不带前导零，做二次宽松匹配
    for key, cell in layout.items():
        if cell.get("type") == "seat" and str(cell.get("name", "")).strip() == str(seat_no).strip():
            return cell["id"]
    logger.warning("未找到座位 %s（阅览室 %s）", seat_no, room_id)
    return None


def _to_minutes(hhmm: str) -> int:
    """'08:00' -> 480（距零点分钟数，接口要求）。"""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _resolve_date(date_expr: str) -> str:
    """'tomorrow' / 'today' / 'YYYY-MM-DD' -> 'YYYY-MM-DD'。"""
    if date_expr in ("tomorrow", "next", ""):
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    if date_expr == "today":
        return datetime.now().strftime("%Y-%m-%d")
    return date_expr


def free_book(token: str, seat_id: int, start: str, end: str,
              date: str) -> Dict:
    """提交预约。返回完整接口响应（让上层判断是否需要滑块）。"""
    payload = {
        "token": token,
        "startTime": _to_minutes(start),
        "endTime": _to_minutes(end),
        "seat": seat_id,
        "date": _resolve_date(date),
    }
    logger.info("提交预约: seat=%s %s %s~%s", seat_id, payload["date"],
                start, end)
    return _http_post("/rest/v2/freeBook", payload)


def is_captcha_blocked(resp: Dict) -> bool:
    """根据接口响应判断是否被滑块验证码拦截。"""
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")) + str(resp.get("code", ""))
    lowered = msg.lower()
    keywords = ["验证", "滑块", "captcha", "slider", "verify", "人机",
                "滑动", "安全验证", "行为验证"]
    return any(k in lowered for k in keywords)


def reservations(token: str) -> Dict:
    """查看我的预约（可用于校验结果）。"""
    return _http_get("/rest/v2/user/reservations", {"token": token})
