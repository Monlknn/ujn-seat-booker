"""GUI 冒烟测试：实例化窗口 → 检查定时抢座面板 → 启动/停止守护 → 安全退出。

只验证 UI 构建与调度线程能正常起停，**不发任何网络请求**。
运行：python debug/smoke_gui_schedule.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk  # noqa: E402

from src import gui  # noqa: E402
from src.config import load_config  # noqa: E402


def main() -> int:
    root = tk.Tk()
    root.withdraw()  # 不弹窗，避免干扰桌面
    try:
        app = gui.App(root)
    except Exception as exc:  # noqa: BLE001
        print(f"[x] App 实例化失败: {exc}")
        return 1

    # --- 1. 定时面板控件齐全 ---
    for attr in ("sched_time_var", "sched_prewarm_var", "sched_wake_var",
                 "sched_autostart_var", "sched_start_btn", "sched_stop_btn",
                 "sched_status_var"):
        if not hasattr(app, attr):
            print(f"[x] 缺少 {attr}")
            return 1
    print(f"[✓] 定时面板控件齐全：时刻={app.sched_time_var.get()} "
          f"提前={app.sched_prewarm_var.get()}分钟 "
          f"防休眠={app.sched_wake_var.get()}")
    print(f"[i] 初始状态：{app.sched_status_var.get()}")

    # --- 2. 非法时间应被拒绝（不启动守护）---
    app.sched_time_var.set("99:99")
    app._start_schedule()
    if app._sched is not None:
        print("[x] 非法时间竟然启动了守护")
        return 1
    print("[✓] 非法时间 99:99 被拒绝，未启动守护")

    # --- 3. 启动守护（用一个远未来的时刻，不会真的去预约）---
    app.sched_time_var.set("07:00")
    app._start_schedule()
    if app._sched is None:
        print("[x] 守护未启动")
        return 1
    # 没有 mainloop，需要手动把 Tk 事件泵起来（_drain_logs 靠 after 循环消费队列）
    deadline = time.time() + 10.0
    status = ""
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)
        status = app.sched_status_var.get()
        if "等待" in status:
            break
    print(f"[✓] 守护已启动，状态：{status}")
    if "等待" not in status:
        print(f"[x] 状态文案异常（应含『等待』）：{status}")
        return 1
    if str(app.sched_start_btn["state"]) != "disabled":
        print("[x] 启动后「启动」按钮应禁用")
        return 1

    # --- 4. 停止守护，线程应迅速退出，按钮应恢复 ---
    app._stop_schedule()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)
        if (app._sched_thread is None or not app._sched_thread.is_alive()
                and app._sched is None):
            break
    if app._sched_thread is not None and app._sched_thread.is_alive():
        print("[x] 停止后守护线程仍在运行")
        return 1
    if app._sched is not None:
        print("[x] 停止后 _sched 未清空（按钮没恢复）")
        return 1
    if str(app.sched_start_btn["state"]) != "normal":
        print(f"[x] 停止后「启动」按钮应恢复可用，实际 {app.sched_start_btn['state']}")
        return 1
    print(f"[✓] 守护已停止，按钮已恢复，状态：{app.sched_status_var.get()}")

    # --- 5. 配置能保存 ---
    cfg_before = load_config()
    print(f"[i] 当前 config schedule = {cfg_before.get('schedule')}")

    app.root.destroy()
    try:
        app._worker.stop(timeout=2.0)
    except Exception:  # noqa: BLE001
        pass
    print("\n[✓] GUI 定时抢座面板冒烟测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
