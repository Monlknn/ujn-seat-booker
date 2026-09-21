"""命令行入口。

子命令：
  once              执行一次预约（按 config.json）
  serve [HH:MM]    定时守护：每天 HH:MM（默认 07:00）预约次日座位
                   提前 --prewarm 分钟预热登录并保活页面，准点直接提交
                   默认同时阻止系统睡眠（--no-wake-lock 可关闭）
  wake [秒数]      自检防休眠：持有锁 N 秒（默认 30），期间系统不会睡
  rooms             打印可用阅览室列表（用于确认 room 配置）
  seats <阅览室名>  打印某阅览室的座位（用于确认 seat 配置）
  validate-config  校验 config.json 是否填写完整

用法示例：
  python -m src.cli once
  python -m src.cli serve 07:00                 # 每天 07:00 抢座，提前 2 分钟预热
  python -m src.cli serve 07:00 --prewarm 2     # 同上（显式指定提前量）
  python -m src.cli serve 07:00 --once          # 只跑今天一次就退出
  python -m src.cli wake 30                     # 验证防休眠是否生效
  python -m src.cli rooms
  python -m src.cli seats 第一阅览室
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许以 `python -m src.cli` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.logger import logger, set_debug  # noqa: E402
from src import api as rest  # noqa: E402
from src import booker  # noqa: E402


def _print_rooms(cfg):
    token = rest.get_token(cfg.username, cfg.password)
    if not token:
        return
    rooms = rest.get_rooms(token)
    print(f"共 {len(rooms)} 个阅览室：")
    for r in rooms:
        print(f"  roomId={r['roomId']:>3}  floor={r.get('floor')}  "
              f"name={r.get('room')}  余位={r.get('free')}/{r.get('totalSeats')}")


def _print_seats(cfg, room_name):
    token = rest.get_token(cfg.username, cfg.password)
    if not token:
        return
    room_id = rest.get_room_id(token, room_name)
    if not room_id:
        return
    date = rest._resolve_date(cfg.get("date", "tomorrow"))
    resp = rest._http_get(f"/rest/v2/room/layoutByDate/{room_id}/{date}",
                          {"token": token})
    if resp.get("status") != "success":
        print("获取座位失败:", resp.get("message"))
        return
    layout = resp.get("data", {}).get("layout", {})
    seats = [(k, v.get("name"), v.get("status")) for k, v in layout.items()
             if v.get("type") == "seat"]
    print(f"{room_name}（{len(seats)} 个座位），样例：")
    for k, name, status in seats[:30]:
        print(f"  seatId={k}  编号={name}  状态={status}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="济南大学图书馆座位自动预约")
    parser.add_argument("command", choices=["once", "serve", "rooms", "seats",
                                            "validate-config", "gui", "wake"])
    parser.add_argument("arg", nargs="?", default=None,
                        help="seats 子命令的阅览室名 / serve 的 HH:MM / wake 的秒数")
    parser.add_argument("-c", "--config", default=None, help="config.json 路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="调试模式：拦截 freeBook 请求，不提交真实预约")
    parser.add_argument("--debug", action="store_true",
                        help="强制开启 debug 输出与截图保存")
    parser.add_argument("--prewarm", type=int, default=2, metavar="MIN",
                        help="serve：提前几分钟预热登录（默认 2；济大 SSO 约 8~10 秒，"
                             "1~2 分钟绰绰有余）")
    parser.add_argument("--max-late", type=int, default=90, metavar="MIN",
                        help="serve：迟到超过几分钟就视为睡过头，跳过当天（默认 90；"
                             "与 config.schedule.max_late_minutes 默认值对齐。"
                             "注意 07:10 这类「迟到 10 分钟」属于容忍窗口内，会立即补跑）")
    parser.add_argument("--max-wait", type=int, default=60, metavar="MIN",
                        help="serve --once：距离下次目标超过几分钟就判定「离得太远」，"
                             "不做无谓等待直接退出（默认 60）。避免计划任务补跑时"
                             "傻等一整晚被强杀（0xC0000142）并占住任务实例")
    parser.add_argument("--once", action="store_true",
                        help="serve：只跑今天这一次，完成后退出（不循环到明天）")
    parser.add_argument("--no-wake-lock", action="store_true",
                        help="serve：不阻止系统睡眠（默认阻止；若电脑靠电池且需要"
                             "合盖休眠可关，但那样定时任务大概率不会触发）")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg._data["dry_run"] = bool(args.dry_run)
    if args.debug:
        cfg._data["debug"] = True
    if cfg.get("dry_run"):
        print("[dry-run] 已启用：将拦截 freeBook，不会留下真实预约记录（脚本跑通会报「成功」，即虚拟成功）")
    set_debug(cfg.get("debug", False))

    if args.command == "validate-config":
        missing = [k for k in ("username", "password", "room", "seat", "start", "end")
                   if not cfg.get(k)]
        if missing:
            print("配置不完整，缺少：", missing)
            return 1
        print("配置校验通过 ✓")
        # 打印「实际生效」的时间窗：周二闭馆会自动换成 08:30~12:00，
        # 直接看 start/end 会以为配置被改了。
        _w_start, _w_end = cfg.time_window()
        print(f"预约目标：{cfg.get('room')} / 座位 {cfg.get('seat')} / "
              f"{cfg.target_date()} {_w_start}~{_w_end}")
        _w_note = cfg.time_window_note()
        if _w_note:
            print(f"⚠ {_w_note}")
        return 0

    if args.command == "rooms":
        _print_rooms(cfg)
        return 0

    if args.command == "seats":
        name = args.arg or cfg.get("room")
        _print_seats(cfg, name)
        return 0

    if args.command == "once":
        ok = booker.book_once(cfg)
        print("预约结果:", "成功" if ok else "失败")
        return 0 if ok else 1

    if args.command == "gui":
        from src.gui import main as gui_main
        gui_main()
        return 0

    if args.command == "serve":
        open_time = args.arg or "07:00"
        try:
            from src.scheduler import parse_hhmm
            h, m = parse_hhmm(open_time)
        except ValueError as exc:
            print(f"时间格式错误：{exc}（应为 HH:MM，如 07:00）")
            return 1
        print(f"[定时] 每天 {h:02d}:{m:02d} 抢座，提前 {args.prewarm} 分钟预热登录"
              f"；{'阻止' if not args.no_wake_lock else '不阻止'}系统睡眠"
              f"；{'只跑一次' if args.once else '每天循环'}。Ctrl+C 退出。")
        try:
            booker.serve(cfg, f"{h:02d}:{m:02d}",
                         prewarm_minutes=args.prewarm,
                         keep_awake=not args.no_wake_lock,
                         max_late_minutes=args.max_late,
                         run_once=args.once,
                         max_wait_minutes=args.max_wait)
        except KeyboardInterrupt:
            print("\n已退出定时模式")
        return 0

    if args.command == "wake":
        from src.wake import WakeLock, _self_test
        try:
            secs = float(args.arg) if args.arg else 30.0
        except ValueError:
            print("wake 的参数应为秒数，如: python -m src.cli wake 30")
            return 1
        if not WakeLock().supported:
            print(f"[!] 当前平台不支持自动阻止睡眠：{WakeLock().describe()}")
            print("    请手动把电源计划设为「从不睡眠」，否则定时任务可能不触发。")
            return 1
        return _self_test(secs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
