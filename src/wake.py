"""防止操作系统在等待期间进入睡眠 / 休眠 / 关屏断电状态。

为什么要这个模块
----------------
定时抢座守护进程要挂机等到次日 07:00。Windows 默认 10~30 分钟无操作就睡眠，
一睡 `time.sleep()` 就不再推进（系统挂起期间墙上时钟不走/定时器不触发），
早上 7 点根本没人执行 —— 这是"定时脚本跑不了"最常见的根因。

借鉴辽大抢座项目的做法：等待期间**主动向系统申请"别睡"**，抢座完成后立即释放，
让电脑恢复正常电源策略（不会白白耗电一整天）。

实现
----
- Windows : `kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED)`，并用心跳线程每 50 秒
            重新声明一次（部分电源策略/远程桌面会话会清掉标志，刷新更稳）。
- macOS   : `caffeinate -dimsu` 子进程（终止即释放）。
- Linux   : `systemd-inhibit` 优先，退回 `xdg-screensaver suspend`。
- 不支持  : 静默降级为"不阻止睡眠"，只打一条警告日志，不影响主流程。

用法
----
    from src.wake import WakeLock
    with WakeLock("ujn-seat-booker") as lock:
        ...  # 等待期间系统不会睡
    # 退出 with 立即恢复原电源策略

或手动 acquire() / release()。
"""
from __future__ import annotations

import ctypes
import platform
import subprocess
import sys
import threading
import time
from typing import Optional

from .logger import logger

SYSTEM = platform.system()  # Windows / Darwin / Linux


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
# https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
ES_CONTINUOUS = 0x80000000        # 标志持续有效，直到再次调用清除
ES_SYSTEM_REQUIRED = 0x00000001   # 阻止系统进入睡眠
ES_DISPLAY_REQUIRED = 0x00000002  # 阻止显示器关闭
ES_AWAYMODE_REQUIRED = 0x00000040 # 允许进入"离开模式"：屏幕可关、系统仍运行

_WIN_FLAGS = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED
              | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED)


def _win_set(flags: int) -> bool:
    """调用 SetThreadExecutionState。返回是否成功。"""
    try:
        rc = ctypes.windll.kernel32.SetThreadExecutionState(
            ctypes.c_uint(flags))
        return bool(rc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("SetThreadExecutionState 失败: %s", exc)
        return False


# --------------------------------------------------------------------------
# macOS / Linux 子进程句柄
# --------------------------------------------------------------------------
def _spawn(argv: list) -> Optional[subprocess.Popen]:
    try:
        return subprocess.Popen(argv,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        logger.debug("启动 %s 失败: %s", argv[0], exc)
        return None


class WakeLock:
    """跨平台"阻止系统睡眠"锁。不支持的平台会静默降级（不报错）。

    参数
    ----
    reason    : 声明给系统的原因字符串（Windows 只记录不看，Linux 会显示）。
    heartbeat : Windows 心跳间隔秒数。建议 < 60，默认 50。
                系统可能因远程桌面断开、电源策略变更清掉标志，周期刷新最稳。
    """

    def __init__(self, reason: str = "ujn-seat-booker 定时抢座",
                 heartbeat: float = 50.0):
        self.reason = reason
        self.heartbeat = heartbeat
        self._proc: Optional[subprocess.Popen] = None
        self._timer: Optional[threading.Timer] = None
        self._held = False
        self._lock = threading.Lock()

    # ---------------- 平台能力 ----------------
    @property
    def supported(self) -> bool:
        """当前平台是否支持阻止睡眠。"""
        if SYSTEM == "Windows":
            return True
        if SYSTEM == "Darwin":
            return _has_cmd("caffeinate")
        if SYSTEM == "Linux":
            return _has_cmd("systemd-inhibit") or _has_cmd("xdg-screensaver")
        return False

    def describe(self) -> str:
        """人话描述当前平台会用什么方式阻止睡眠（用于 GUI / 启动日志）。"""
        if SYSTEM == "Windows":
            return "Windows：SetThreadExecutionState（阻止睡眠 + 离开模式）"
        if SYSTEM == "Darwin":
            return ("macOS：caffeinate 阻止睡眠" if _has_cmd("caffeinate")
                    else "macOS：未找到 caffeinate，无法阻止睡眠")
        if SYSTEM == "Linux":
            if _has_cmd("systemd-inhibit"):
                return "Linux：systemd-inhibit 阻止睡眠"
            if _has_cmd("xdg-screensaver"):
                return "Linux：xdg-screensaver suspend 阻止屏保"
            return "Linux：未找到 systemd-inhibit / xdg-screensaver，无法阻止睡眠"
        return f"未知平台 {SYSTEM}：不阻止睡眠"

    # ---------------- 生命周期 ----------------
    def acquire(self) -> bool:
        """申请"别睡"。重复调用是安全的（幂等）。"""
        with self._lock:
            if self._held:
                return True
            ok = self._acquire_platform()
            self._held = ok
            if ok:
                logger.info("已阻止系统睡眠（%s）", self.describe())
                self._schedule_heartbeat()
            else:
                logger.warning("无法阻止系统睡眠：%s。"
                               "请手动把电源计划设为「从不睡眠」，否则定时任务可能不触发。",
                               self.describe())
            return ok

    def release(self) -> None:
        """恢复系统正常电源策略。重复调用安全。"""
        with self._lock:
            if not self._held:
                return
            self._held = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._release_platform()
        logger.info("已恢复系统正常电源策略（不再阻止睡眠）")

    @property
    def held(self) -> bool:
        return self._held

    # ---------------- with 语法 ----------------
    def __enter__(self) -> "WakeLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ---------------- 平台实现 ----------------
    def _acquire_platform(self) -> bool:
        if SYSTEM == "Windows":
            return _win_set(_WIN_FLAGS)

        if SYSTEM == "Darwin":
            # -d 防显示器睡眠 -i 防系统空闲睡眠 -m 防磁盘睡眠
            # -s 防系统睡眠（需接电源） -u 声明用户活动
            self._proc = _spawn(["caffeinate", "-dimsu", "-w", str(_self_pid())])
            return self._proc is not None

        if SYSTEM == "Linux":
            if _has_cmd("systemd-inhibit"):
                self._proc = _spawn([
                    "systemd-inhibit",
                    "--what=handle-lid-switch:sleep:idle",
                    f"--who={_self_pid()}",
                    f"--why={self.reason}",
                    "sleep", "infinity",
                ])
                if self._proc is not None:
                    return True
            if _has_cmd("xdg-screensaver"):
                self._proc = _spawn(["xdg-screensaver", "suspend", str(_self_pid())])
                return self._proc is not None
            return False

        return False

    def _release_platform(self) -> None:
        if SYSTEM == "Windows":
            # 再次调用并只带 ES_CONTINUOUS = 清除之前设置的所有标志
            _win_set(ES_CONTINUOUS)
            return
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None

    # ---------------- 心跳 ----------------
    def _schedule_heartbeat(self) -> None:
        """Windows 上周期性重新声明（防御标志被系统清掉）。"""
        if SYSTEM != "Windows":
            return
        self._timer = threading.Timer(self.heartbeat, self._on_heartbeat)
        self._timer.daemon = True
        self._timer.start()

    def _on_heartbeat(self) -> None:
        with self._lock:
            if not self._held:
                return
            _win_set(_WIN_FLAGS)  # 刷新
            self._schedule_heartbeat()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _self_pid() -> int:
    import os
    return os.getpid()


def _has_cmd(name: str) -> bool:
    from shutil import which
    return which(name) is not None


# --------------------------------------------------------------------------
# 计划任务探测：GUI 守护与 Windows 计划任务是两套独立机制，同时开会重复抢座
# --------------------------------------------------------------------------
def list_installed_task_names(base_name: str = "UJN-Seat-Booker") -> list:
    """返回本机已安装的抢座计划任务名列表（主任务 + 补跑）。

    非 Windows / schtasks 不可用 / 查询失败 → 返回 []，调用方当作"没装"处理，
    绝不因为探测失败就拦住用户启动守护。

    为什么需要：计划任务带 WakeToRun，能唤醒睡眠的电脑；GUI 守护只是个 Python
    进程，电脑一睡它也睡。两套同时开会各抢一次 —— 虽然重复预约是幂等安全的
    （服务端返回「已有有效预约」判成功），但会多跑一次浏览器登录，
    白白浪费 10 秒并增加被风控的概率。
    """
    if sys.platform != "win32":
        return []
    found = []
    try:
        kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        for name in (base_name, base_name + "-BK1", base_name + "-BK2"):
            try:
                r = subprocess.run(
                    ["schtasks", "/query", "/tn", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8, **kwargs)
                if r.returncode == 0:
                    found.append(name)
            except Exception:  # noqa: BLE001
                break  # schtasks 不可用就别继续循环了
    except Exception:  # noqa: BLE001
        return []
    return found


# --------------------------------------------------------------------------
# 自检：python -m src.wake  → 持有 30 秒锁，期间系统不会睡
# --------------------------------------------------------------------------
def _self_test(seconds: float = 30.0) -> int:
    lock = WakeLock()
    if not lock.supported:
        print(f"[!] 当前平台不支持阻止睡眠：{lock.describe()}")
        return 1
    print(f"[i] {lock.describe()}")
    print(f"[i] 现在持有锁 {seconds:.0f} 秒，期间系统不会睡眠（可以观察电源设置）。Ctrl+C 提前结束。")
    lock.acquire()
    try:
        t0 = time.time()
        while time.time() - t0 < seconds:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[i] 手动中断")
    finally:
        lock.release()
    print("[✓] 已释放，系统恢复正常电源策略")
    return 0


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    sys.exit(_self_test(secs))
