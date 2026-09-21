"""济南大学座位预约 —— 桌面 GUI（Tkinter，零额外依赖，类似辽大的桌面 GUI）。

用法：
    python -m src.gui
    # 或在 cli 里：python -m src.cli gui

功能：
  - 账号 / 校区 / 楼层 / 阅览室 / 座位 / 日期 / 起止时间 / 模式 / 浏览器通道 均可界面选择。
  - 校区与楼层、楼层与阅览室是级联关系：选校区后自动刷新楼层选项，选楼层后自动刷新阅览室。
  - 三个校区楼层集合不同：主校区 2-7 层、舜耕 1-5 层、多功能区（主校区）2 层。
  - 「刷新阅览室」「刷新空闲座位」联网从官网拉取可选项（读页面 DOM，绕开 frontApi 的 HMAC 签名）。
  - 「开始预约(真实)」一键真实下单；「Dry-run 测试」只验证流程不真正占座。
  - 「保存配置」把当前界面选项写回 config.json。
  - 底部实时日志。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from src.config import load_config, Config, PROJECT_ROOT
from src import booker
from src.logger import logger
from src.scheduler import DailyScheduler

CONFIG_PATH = PROJECT_ROOT / "config.json"
TIME_OPTIONS = [f"{h:02d}:{m:02d}" for h in range(7, 23) for m in (0, 30)]


class _BookWorker(threading.Thread):
    """长驻后台线程：所有 BrowserSession / Playwright 操作都在这里执行。

    关键设计（Playwright sync_api 跨线程限制）：
        Playwright 的 sync_api 必须在创建 BrowserContext 的同一线程里使用，
        跨线程用同一 BrowserContext 会报 "no running event loop"。本类把
        login_browser / booker.book_once_with_page / page.evaluate 全部串行
        跑在同一个 daemon 线程里，GUI 主线程通过 _task_event / _task_done 派
        发任务、获取结果。

    收益：
        prewarm 完成后 BrowserSession **保活**，do_book 直接复用 page，
        省去 pw.launch + new_context + cookie 注入 / 12 秒 cookie 轮询。
        实测 cold start（prewarm 不开）= 14s，复用 prewarm = 3s，节约 11s。

    任务派发（同步等待）：
        result = worker.submit("prewarm", cfg)   # 同步阻塞直到 worker 完成
        result = ("prewarm_ok", True)
        result = worker.submit("book", cfg, dry=False, timeout=120.0)
        result = ("book_ok", True) / ("error", "...") / ("timeout", None)

    并发保证：
        - 任务串行：worker 一次只跑一个任务，submit 时若上一任务没完成 → 返回 ("busy", None)
        - BrowserSession 操作都在 worker 线程，无跨线程风险
    """

    def __init__(self):
        super().__init__(daemon=True, name="BookWorker")
        self._task_event = threading.Event()
        self._task_done = threading.Event()
        self._task_done.set()  # 初始无任务在跑
        self._task_args: tuple | None = None  # (kind, cfg, dry, ...)
        self._task_result: tuple | None = None
        self._sess = None  # BrowserSession 保活
        self._stopping = False

    def run(self):
        """主循环：等任务 → 执行 → 标记完成 → 循环。"""
        while not self._stopping:
            self._task_event.wait()
            self._task_event.clear()
            if self._stopping:
                break

            args = self._task_args
            self._task_args = None
            self._task_result = None
            try:
                self._task_result = self._execute(args or ("error", None, False))
            except Exception as exc:  # noqa: BLE001
                logger.error("worker 任务异常: %s", exc, exc_info=True)
                self._task_result = ("error", str(exc))
            finally:
                self._task_done.set()

    def has_alive_sess(self) -> bool:
        """当前 sess 是否可用。**只能在 worker 线程内调用**（触碰 Playwright 对象）。

        其它线程想知道就派发 "ping" 任务，不要直接读 self._sess。
        """
        if self._sess is None:
            return False
        try:
            return bool(self._sess.alive)
        except Exception:  # noqa: BLE001
            return False

    def _execute(self, args):
        """在 worker 线程里跑：登录（如需）→ 执行任务。"""
        kind, cfg, dry = args[0], args[1], (len(args) > 2 and args[2])
        from src.session import login_browser
        # ping：只探活，不登录（定时守护等待期间的保活检查用）
        if kind == "ping":
            return ("pong", self.has_alive_sess())
        # ★ 复用旧 sess（避免 cold start 的 8~12 秒）
        if self._sess is None or not self._sess.alive:
            logger.info("worker 创建登录 BrowserSession ...")
            self._sess = login_browser(cfg)
        else:
            logger.debug("worker 复用已有 BrowserSession（alive=True）")

        if kind == "prewarm":
            # ★ 预热阶段就把页面停到首页并等房间卡片渲染好：
            #   济大登录完停在 SSO 跳转页，不先回首页的话准点提交会找不到房间卡片，
            #   而且这几秒渲染等待不该留到开放之后。
            try:
                if self._sess.ensure_home():
                    logger.debug("预热页面已停在首页，房间卡片已渲染")
                else:
                    logger.warning("预热页面未能回到首页，提交时会再试一次")
            except Exception as exc:  # noqa: BLE001
                logger.debug("预热 ensure_home 异常: %s", exc)
            return ("prewarm_ok", True)
        elif kind == "book":
            ok = booker.book_once_with_page(self._sess.page, cfg, sess=self._sess)
            return ("book_ok", ok)
        elif kind == "close":
            # 主动丢弃 sess（下次 submit("prewarm"/"book") 会重新登录）
            if self._sess is not None:
                try:
                    self._sess.close()
                except Exception:  # noqa: BLE001
                    pass
                self._sess = None
            return ("close_ok", True)
        else:
            return ("error", f"unknown kind: {kind}")

    def submit(self, kind, cfg, dry=False, timeout: float = 300.0):
        """派发任务到 worker 并同步等待结果（任意线程调用都安全）。

        返回值是 (status, payload) 元组，常见：
          - ("prewarm_ok", True) / ("book_ok", True) / ("book_ok", False)
          - ("busy", None)  上一任务还没跑完
          - ("timeout", None) 等待超时
          - ("error", "...")  任务执行时抛异常
          - None  worker 已停止
        """
        if self._stopping:
            return None
        if not self._task_done.is_set():
            return ("busy", None)
        self._task_args = (kind, cfg, dry)
        self._task_done.clear()
        self._task_event.set()
        if not self._task_done.wait(timeout=timeout):
            return ("timeout", None)
        return self._task_result

    def is_busy(self) -> bool:
        """是否有任务在跑（GUI 主线程用来决定要不要 showwarning）。"""
        return not self._task_done.is_set()

    def close_sess(self):
        """同步关闭当前 BrowserSession（GUI 退出时 / 失效时调用）。

        通过派发 "close" 任务实现，关闭操作也在 worker 线程里完成，规避
        跨线程 BrowserContext 关闭风险。
        """
        if self._sess is None:
            return
        if self._stopping:
            # 已在退出流程里直接关（stop() 已处理）
            return
        # 派发 close 任务，等 worker 处理；超时也不阻塞主线程太久
        self.submit("close", cfg=None, timeout=5.0)

    def stop(self, timeout: float = 5.0):
        """停止 worker 线程（GUI 退出时调用）。同时关闭任何残留 sess。"""
        self._stopping = True
        self._task_event.set()
        # 关 sess（如有）
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._sess = None
        self.join(timeout=timeout)

# 三个校区并集后的楼层候选（覆盖所有可能的值）。
# 实际启用哪些由官网动态返回（每个校区不同）；这里只是把选项都展示给用户点。
ALL_FLOOR_OPTIONS = ["全部", "1层", "2层", "3层", "4层", "5层", "6层", "7层"]
DEFAULT_CAMPUS_OPTIONS = ["主校区", "舜耕校区", "多功能区（主校区）"]

# 各校区的楼层集合（探查结果：主校区 2-7 层、舜耕 1-5 层、多功能区 2 层）
CAMPUS_FLOOR_HINT = {
    "主校区": ["全部", "2层", "3层", "4层", "5层", "6层", "7层"],
    "舜耕校区": ["全部", "1层", "2层", "3层", "4层", "5层"],
    "多功能区（主校区）": ["全部", "2层"],
}

# 注意：济大座位系统中「阅览室编号」与「所在楼层」**不一致**（实际探测结果）：
#   - 第一阅览室 → 2 层
#   - 第二阅览室（北/中/南） → 3 层
#   - 第三阅览室（北/中/南） → 4 层
#   - 第四阅览室（北/中/南） → 5 层
#   - 第五阅览室（北/中/南） → 6 层
#   - 第六、七…阅览室 同样 ≠ 楼层号
# 所以之前的 FLOOR_NAME_MAP（拿名字里的汉字猜楼层）是**错的**。改用接口响应
# 里的 `floorName` 字段做权威过滤（见 refresh_rooms 内的接口拦截逻辑）。
FLOOR_NAME_MAP = {}  # 保留 dict 占位，防止历史脚本 import；不再用于过滤

# 日期下拉：界面显示中文，落库/下发仍用后端认识的 today/tomorrow
DATE_DISPLAY_TO_VALUE = {"今天": "today", "明天": "tomorrow"}
DATE_VALUE_TO_DISPLAY = {v: k for k, v in DATE_DISPLAY_TO_VALUE.items()}
DATE_DISPLAY_OPTIONS = ["今天", "明天"]

# 济大座位系统的时段规则（★ 2026-09-04 修正：之前我误以为「系统固定 07:00」，
# 实际是点击座位后**弹窗**里有「开始时间 / 结束时间」两个 el-select 下拉）：
#   - 开始时间：07:00 起，30 分钟步长，到 22:00（与结束时间共用同一组选项）
#   - 结束时间：必须 > 开始时间；可设到 22:30
#   - Vue 实现：先选开始时间会触发 vm.getEndTimeByStartime() 重算 endTimes 列表，
#     **反过来**先选结束再选开始会被重置——所以 booker 必须先设 starTimeValue。
# 注：TIME_OPTIONS 已在文件头部定义（line 33），这里仅补充注释。
COLOR_BG = "#F5F7FA"
COLOR_CARD = "#FFFFFF"
COLOR_ACCENT = "#2563EB"
COLOR_ACCENT_HOVER = "#1D4ED8"
COLOR_TEXT = "#1F2937"
COLOR_MUTED = "#6B7280"
COLOR_BORDER = "#E5E7EB"
COLOR_SUCCESS = "#10B981"
COLOR_WARN = "#F59E0B"
COLOR_DANGER = "#EF4444"


# 日志队列：后台线程只负责「入队」，主线程定时「出队」刷新文本框。
# 这样可以彻底避免「后台线程直接调 Tk 组件 + Playwright 霸占 GIL 导致 UI 冻结/延迟」。
LOG_QUEUE = queue.Queue()


class QueueLogHandler(logging.Handler):
    """把日志行塞进线程安全队列，由主线程的 _drain_logs 定时取出刷新。"""

    def emit(self, record):
        try:
            msg = self.format(record) + "\n"
            LOG_QUEUE.put(msg)
        except Exception:  # noqa: BLE001
            pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("济南大学图书馆座位预约 · WorkBuddy")
        root.geometry("980x1020")   # 高度含「定时抢座」卡片
        root.minsize(840, 860)
        root.configure(bg=COLOR_BG)
        # clam 主题支持自定义按钮配色，观感比默认主题更现代（vista 会忽略按钮背景色）
        try:
            style = ttk.Style()
            if "clam" in style.theme_names():
                style.theme_use("clam")
            elif "vista" in style.theme_names():
                style.theme_use("vista")
            self._style = style
        except Exception:
            self._style = None
        self._configure_styles()

        try:
            self.cfg = load_config()
        except Exception:
            self.cfg = Config({})
        self.vars: dict[str, tk.StringVar] = {}
        self.combos: dict[str, ttk.Combobox] = {}
        # 防级联递归标志
        self._cascading = False
        # ★ 长驻 worker 线程：所有 BrowserSession / Playwright 操作都在这里执行。
        # prewarm 和 do_book 都通过 _worker 派发，worker 保活 BrowserSession，
        # 第二次预约（do_book）复用同一 page —— 避免 pw.launch + new_context
        # + cookie 注入 / 12 秒轮询，节约 8~12 秒。
        # Playwright sync_api 不能跨线程用 BrowserContext，所以统一在 worker 线程里。
        self._worker = _BookWorker()
        self._worker.start()

        # ★ 守护线程 → 主线程的单向状态通道。
        #   Tk 只能在创建它的线程（主线程）里操作，后台线程直接调 root.after /
        #   StringVar.set 会**死锁**（实测：冒烟测试直接卡死）。所以后台线程只往
        #   这个队列里丢字符串，由主线程已有的 _drain_logs 循环取出来更新 UI。
        #   "@@STOPPED@@" 是约定好的哨兵，表示守护线程已退出。
        self._sched_status_queue = queue.Queue()

        # ---- 定时抢座守护 ----
        # config.json 的 schedule 段持久化这些设置；勾选「打开本程序时自动挂上守护」
        # 后，下次启动 GUI 会直接开始等待，不用每次手动点。
        _sc = self.cfg.get("schedule") or {}
        self.sched_autostart_var = tk.BooleanVar(value=bool(_sc.get("enabled", False)))
        self.sched_time_var = tk.StringVar(value=str(_sc.get("open_time", "07:00")))
        self.sched_prewarm_var = tk.IntVar(value=int(_sc.get("prewarm_minutes", 2)))
        self.sched_wake_var = tk.BooleanVar(value=bool(_sc.get("keep_awake", True)))
        self._sched: "DailyScheduler | None" = None
        self._sched_thread = None
        self._sched_wake = None
        # ★ Tk 变量不能跨线程读（Tk 非线程安全），守护线程要用的一切都在启动时快照
        self._sched_cfg: "Config | None" = None
        self._sched_keep_awake = True
        self._sched_have_sess = False
        self._sched_last_ping = 0.0
        self._sched_last_ui = 0.0

        self._build_ui()
        handler = QueueLogHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("GUI 已启动，配置路径: %s", CONFIG_PATH)
        # 启动主线程日志刷新循环（不再由后台线程直接碰 Tk 组件）
        self._drain_logs()

        # ----------------------------- 主题 -----------------------------
    def _configure_styles(self):
        s = self._style
        if s is None:
            return
        try:
            s.configure("TFrame", background=COLOR_BG)
            s.configure("Card.TFrame", background=COLOR_CARD, relief="flat")
            s.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT,
                        font=("Segoe UI", 9))
            s.configure("Card.TLabel", background=COLOR_CARD, foreground=COLOR_TEXT,
                        font=("Segoe UI", 9))
            s.configure("Muted.TLabel", background=COLOR_CARD, foreground=COLOR_MUTED,
                        font=("Segoe UI", 8))
            s.configure("Header.TLabel", background=COLOR_BG, foreground=COLOR_ACCENT,
                        font=("Segoe UI Semibold", 14))
            s.configure("SubHeader.TLabel", background=COLOR_CARD, foreground=COLOR_TEXT,
                        font=("Segoe UI Semibold", 10))
            s.configure("TLabelframe", background=COLOR_CARD, foreground=COLOR_TEXT,
                        relief="solid", borderwidth=1)
            s.configure("TLabelframe.Label", background=COLOR_CARD,
                        foreground=COLOR_ACCENT,
                        font=("Segoe UI Semibold", 10))
            s.configure("TEntry", fieldbackground="white", borderwidth=1, relief="solid")
            s.configure("TCombobox", fieldbackground="white", borderwidth=1, relief="solid",
                        padding=2)
            s.map("TCombobox",
                  fieldbackground=[("readonly", "white")],
                  selectbackground=[("readonly", "#E0E7FF")],
                  selectforeground=[("readonly", COLOR_TEXT)])
            s.configure("TCheckbutton", background=COLOR_CARD, foreground=COLOR_TEXT,
                        font=("Segoe UI", 9))
            s.configure("Primary.TButton", font=("Segoe UI Semibold", 9),
                        padding=(14, 6))
            s.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(12, 6))
            s.configure("Danger.TButton", font=("Segoe UI Semibold", 9), padding=(14, 6))
            s.configure("Treeview", rowheight=24, fieldbackground="white",
                        background="white")
            s.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
            # ---- 按钮配色（clam 主题下生效）----
            s.configure("Primary.TButton", background=COLOR_ACCENT,
                        foreground="white", borderwidth=0,
                        font=("Segoe UI Semibold", 9), padding=(16, 8))
            s.map("Primary.TButton",
                  background=[("active", COLOR_ACCENT_HOVER),
                              ("disabled", "#9CA3AF")],
                  foreground=[("disabled", "#F3F4F6")])
            s.configure("Success.TButton", background=COLOR_SUCCESS,
                        foreground="white", borderwidth=0,
                        font=("Segoe UI Semibold", 9), padding=(16, 8))
            s.map("Success.TButton",
                  background=[("active", "#059669"), ("disabled", "#9CA3AF")],
                  foreground=[("disabled", "#F3F4F6")])
            s.configure("Warn.TButton", background=COLOR_WARN,
                        foreground="white", borderwidth=0,
                        font=("Segoe UI Semibold", 9), padding=(14, 8))
            s.map("Warn.TButton",
                  background=[("active", "#D97706"), ("disabled", "#9CA3AF")],
                  foreground=[("disabled", "#F3F4F6")])
            s.configure("Secondary.TButton", background="#EEF2F7",
                        foreground=COLOR_TEXT, borderwidth=0,
                        font=("Segoe UI", 9), padding=(14, 8))
            s.map("Secondary.TButton",
                  background=[("active", "#E2E8F0")])
        except Exception:
            pass

    # ----------------------------- 日志刷新（主线程） -----------------------------
    def _drain_logs(self):
        """主线程定时从 LOG_QUEUE 取日志刷到文本框，UI 始终流畅。

        顺带消费定时守护的状态队列（后台线程只 put，不碰 Tk —— 见
        `self._sched_status_queue` 的注释：后台线程调 Tk 会死锁）。
        """
        try:
            while True:
                line = LOG_QUEUE.get_nowait()
                self.log.configure(state="normal")
                self.log.insert(tk.END, line)
                self.log.configure(state="disabled")
                self.log.see(tk.END)
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            pass
        # 定时守护状态：后台线程 put，这里（主线程）更新 UI
        try:
            while True:
                text = self._sched_status_queue.get_nowait()
                if text == "@@STOPPED@@":
                    self._on_schedule_stopped()
                else:
                    self._sched_set_status(text)
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            pass
        self.root.after(120, self._drain_logs)

    # ----------------------------- UI -----------------------------
    def _build_ui(self):
        # 顶部标题条（实色通栏，观感更像现代应用）
        header = tk.Frame(self.root, bg=COLOR_ACCENT, height=62)
        header.pack(fill="x")
        header.pack_propagate(False)
        inner = tk.Frame(header, bg=COLOR_ACCENT)
        inner.pack(fill="both", expand=True, padx=20)
        tk.Label(inner, text="📚  济南大学图书馆座位预约", bg=COLOR_ACCENT,
                 fg="white", font=("Segoe UI Semibold", 15)).pack(side="left")
        tk.Label(inner,
                 text="v1.2  ·  会话复用提速 · 楼层级联 · 准时抢座",
                 bg=COLOR_ACCENT, fg="#DBEAFE",
                 font=("Segoe UI", 9)).pack(side="left", padx=12)

        # 卡片容器
        body = ttk.Frame(self.root, padding=(18, 14, 18, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        # ---------- Section 1: 账号 ----------
        sec1 = ttk.LabelFrame(body, text="  账号信息  ", padding=14)
        sec1.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)
        self._build_account_section(sec1)

        # ---------- Section 2: 预约条件 ----------
        sec2 = ttk.LabelFrame(body, text="  预约条件（校区 → 楼层 → 阅览室，级联刷新）  ",
                              padding=14)
        sec2.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=6)
        self._build_target_section(sec2)

        # ---------- Section 3: 座位/时间 ----------
        sec3 = ttk.LabelFrame(body, text="  座位与时段  ", padding=14)
        sec3.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=6)
        self._build_seat_time_section(sec3)

        # ---------- Section 4: 浏览器 ----------
        sec4 = ttk.LabelFrame(body, text="  浏览器与模式  ", padding=14)
        sec4.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=6)
        self._build_browser_section(sec4)

        # ---------- Section 5: 定时抢座（跨两列） ----------
        sec5 = ttk.LabelFrame(body, text="  ⏰  定时抢座（每天自动，挂机等到准点）  ",
                              padding=14)
        sec5.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=0, pady=6)
        self._build_schedule_section(sec5)

        # ---------- Action buttons ----------
        btn_frame = ttk.Frame(self.root, padding=(18, 6))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="💾  保存配置", style="Secondary.TButton",
                   command=self.save_config).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="🔄  刷新阅览室", style="Secondary.TButton",
                   command=lambda: self.run_thread(self.refresh_rooms)
                   ).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="🪑  刷新空闲座位", style="Secondary.TButton",
                   command=lambda: self.run_thread(self.refresh_seats)
                   ).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="⚡  预热登录", style="Warn.TButton",
                   command=lambda: self.run_thread(self.prewarm)
                   ).pack(side="left", padx=4)
        ttk.Separator(btn_frame, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(btn_frame, text="✅  开始预约(真实)", style="Success.TButton",
                   command=lambda: self.run_thread(self.do_book, dry=False)
                   ).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="🧪  Dry-run 测试", style="Primary.TButton",
                   command=lambda: self.run_thread(self.do_book, dry=True)
                   ).pack(side="left", padx=4)

        # ---------- Log area ----------
        log_header = ttk.Frame(self.root, padding=(18, 0))
        log_header.pack(fill="x")
        ttk.Label(log_header, text="运行日志", style="SubHeader.TLabel",
                  background=COLOR_BG).pack(side="left")
        ttk.Label(log_header, text="（线程安全、实时滚动）",
                  style="Muted.TLabel", background=COLOR_BG
                  ).pack(side="left", padx=8)
        self.log = scrolledtext.ScrolledText(
            self.root, height=16, state="disabled",
            bg="#0F172A", fg="#E2E8F0",
            insertbackground="#E2E8F0",
            font=("Consolas", 9), relief="flat", borderwidth=0,
        )
        self.log.pack(fill="both", expand=True, padx=18, pady=(4, 6))

        # ---------- 底部状态条：实时汇总当前预约目标 ----------
        self.status_var = tk.StringVar(value="就绪")
        status = tk.Frame(self.root, bg="#E5E7EB", height=26)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        tk.Label(status, textvariable=self.status_var, bg="#E5E7EB",
                 fg=COLOR_TEXT, font=("Segoe UI", 9), anchor="w"
                 ).pack(side="left", padx=12)

        # 任一字段变化即刷新状态条
        for var in self.vars.values():
            var.trace_add("write", self._update_status)
        self._update_status()

        # 上次勾了「打开本程序时自动挂上守护」→ 直接开始等待
        if self.sched_autostart_var.get():
            self.root.after(600, self._start_schedule)

    def _row(self, parent, row, label, widget, hint=None):
        """把「label + widget + 可选 hint」三段一行。"""
        ttk.Label(parent, text=label, style="Card.TLabel"
                  ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel"
                      ).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        parent.columnconfigure(1, weight=1)

    def _make_field(self, parent, key, label, kind, hint=None, row=0,
                    init_values=None, on_select=None):
        val = self.cfg.get(key, "")
        var = tk.StringVar(value=str(val) if val is not None else "")
        self.vars[key] = var
        if kind == "entry":
            w = ttk.Entry(parent, textvariable=var, width=24)
        else:
            init = [val] if val else []
            if init_values:
                init = list(init_values)
            w = ttk.Combobox(parent, textvariable=var, width=22, values=init,
                             state="normal")
            self.combos[key] = w
        if on_select is not None:
            w.bind("<<ComboboxSelected>>", on_select)
        self._row(parent, row, label, w, hint=hint)
        return w

    def _build_account_section(self, parent):
        parent.columnconfigure(1, weight=1)
        # 学号
        uvar = tk.StringVar(value=str(self.cfg.get("username", "")))
        self.vars["username"] = uvar
        ttk.Label(parent, text="学号", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=uvar, width=24).grid(
            row=0, column=1, sticky="ew", pady=4)
        ttk.Label(parent, text="(济大 SSO 账号)",
                  style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=8)
        # 密码
        pvar = tk.StringVar(value=str(self.cfg.get("password", "")))
        self.vars["password"] = pvar
        ttk.Label(parent, text="密码", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=pvar, show="*", width=24).grid(
            row=1, column=1, sticky="ew", pady=4)
        ttk.Label(parent, text="(明文存于本地 config.json)",
                  style="Muted.TLabel").grid(row=1, column=2, sticky="w", padx=8)

    def _build_target_section(self, parent):
        parent.columnconfigure(1, weight=1)
        # 校区（默认有 3 个；启动时若拉到官网真值会覆盖）
        saved = self.cfg.get("campus") or ""
        init_campuses = [c for c in [saved] + DEFAULT_CAMPUS_OPTIONS if c]
        # 去重保序
        seen = set()
        init_campuses = [c for c in init_campuses if not (c in seen or seen.add(c))]
        self._make_field(parent, "campus", "校区", "combo",
                         hint="(场馆选择)",
                         init_values=init_campuses,
                         on_select=self._on_campus_change, row=0)

        # 楼层（默认显示全部校区并集，选项固定「全部」在前，当前值由 var 单独保持）
        self._make_field(parent, "floor", "楼层", "combo",
                         hint="(切换后自动刷新阅览室)",
                         init_values=list(ALL_FLOOR_OPTIONS),
                         on_select=self._on_floor_change, row=1)

        # 阅览室
        saved_room = self.cfg.get("room") or ""
        init_rooms = [r for r in [saved_room] if r]
        self._make_field(parent, "room", "阅览室", "combo",
                         hint="(按当前楼层)",
                         init_values=init_rooms, row=2)

    def _build_seat_time_section(self, parent):
        parent.columnconfigure(1, weight=1)
        self._make_field(parent, "seat", "座位号", "combo",
                         hint="(FREE 优先)", row=0)
        self._make_field(parent, "date", "日期", "combo",
                         hint="(或手填 2026-09-05)",
                         init_values=DATE_DISPLAY_OPTIONS, row=1)
        # 配置里存的是 today/tomorrow，界面统一显示成中文
        self.vars["date"].set(self._date_display(self.cfg.get("date", "")))
        # 开始时间：与结束时间共享 TIME_OPTIONS（07:00~22:30，30 分钟步长）
        # 必须先选开始时间，再选结束时间；否则 Vue 端会把已选的结束时间重置。
        self._make_field(parent, "start", "开始时间", "combo",
                         hint="(先选此项再选结束)",
                         init_values=TIME_OPTIONS, row=2)
        self._make_field(parent, "end", "结束时间", "combo",
                         hint="(必须 > 开始时间)",
                         init_values=TIME_OPTIONS, row=3)
        # 周二闭馆规则提示（动态刷新，见 _update_status）：
        # 图书馆每周二 12:00~16:00 闭馆，无法连约整天，当天自动改约上午。
        self.tuesday_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.tuesday_var, style="Muted.TLabel",
                  justify="left", wraplength=280).grid(
                      row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_browser_section(self, parent):
        parent.columnconfigure(1, weight=1)
        self._make_field(parent, "mode", "运行模式", "combo",
                         init_values=["browser", "api", "auto"],
                         hint="(默认 browser)", row=0)
        self._make_field(parent, "browser_channel", "浏览器通道", "combo",
                         init_values=["msedge", "chrome", "chromium"],
                         hint="(本机已装 Edge)", row=1)

        # 勾选
        self.headless_var = tk.BooleanVar(value=bool(self.cfg.get("headless", True)))
        self.debug_var = tk.BooleanVar(value=bool(self.cfg.get("debug", False)))
        opt = ttk.Frame(parent, style="Card.TFrame")
        opt.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(opt, text="无头浏览器(不弹窗)", variable=self.headless_var
                        ).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opt, text="调试日志", variable=self.debug_var
                        ).pack(side="left")

    def _build_schedule_section(self, parent):
        """定时抢座卡片：每天准点自动预约（提前预热保活 + 准点提交 + 防休眠）。

        流程：提前 N 分钟 → 登录并**保活页面** → 等到准点 → 直接用已登录页面提交。
        等待期间默认阻止系统睡眠，避免电脑一睡定时器就停摆。
        """
        from src.wake import WakeLock
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="每天抢座时刻", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.sched_time_var, width=8).grid(
            row=0, column=1, sticky="w", pady=3)
        ttk.Label(parent, text="HH:MM，济大约 07:00 开放次日预约",
                  style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=8)

        ttk.Label(parent, text="提前预热登录", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=3)
        ttk.Spinbox(parent, from_=1, to=15, width=6,
                    textvariable=self.sched_prewarm_var).grid(
            row=1, column=1, sticky="w", pady=3)
        ttk.Label(parent, text="分钟：先登录并保活页面，准点直接提交（省约 10 秒冷启动）",
                  style="Muted.TLabel").grid(row=1, column=2, sticky="w", padx=8)

        _lock = WakeLock()
        _wake_txt = ("等待期间阻止系统睡眠（" + _lock.describe() + "）"
                     if _lock.supported
                     else "当前平台不支持阻止睡眠，请手动把电源计划设为「从不睡眠」")
        opts = ttk.Frame(parent, style="Card.TFrame")
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        ttk.Checkbutton(opts, text=_wake_txt, variable=self.sched_wake_var,
                        state=("normal" if _lock.supported else "disabled")
                        ).pack(side="left")
        ttk.Checkbutton(opts, text="打开本程序时自动挂上守护",
                        variable=self.sched_autostart_var).pack(side="left", padx=(18, 0))

        btns = ttk.Frame(parent, style="Card.TFrame")
        btns.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.sched_start_btn = ttk.Button(btns, text="⏰  启动定时守护",
                                          style="Primary.TButton",
                                          command=self._start_schedule)
        self.sched_start_btn.pack(side="left")
        self.sched_stop_btn = ttk.Button(btns, text="■  停止",
                                         style="Secondary.TButton",
                                         command=self._stop_schedule,
                                         state="disabled")
        self.sched_stop_btn.pack(side="left", padx=8)
        self.sched_status_var = tk.StringVar(value="未启动")
        ttk.Label(btns, textvariable=self.sched_status_var,
                  style="Card.TLabel").pack(side="left", padx=12)

        # ★ 2026-09-10：用户实测误解过「关窗后守护还在后台跑」。实际 _on_close 会
        # 调 _stop_schedule() 停掉守护并释放 WakeLock，所以 GUI 窗口必须整晚开着 ——
        # 这里把代价写在卡片上，别让人以为设置完就能关窗睡觉。
        ttk.Label(parent,
                  text="⚠ 本窗口必须保持开启：关闭窗口即停止守护（并恢复系统正常电源策略）。\n"
                       "想让电脑睡眠也能抢座 → 改用 Windows 计划任务：以管理员身份运行一次\n"
                       "scripts\\install_windows_task.ps1，之后电脑可正常睡，到点自己醒。",
                  style="Muted.TLabel", justify="left", wraplength=600).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # --------------------------- 定时守护 ---------------------------
    def _start_schedule(self):
        """主线程：校验参数 → 快照配置 → 起守护线程。"""
        if self._sched is not None and not self._sched.stopping:
            messagebox.showinfo("定时守护", "定时守护已在运行中")
            return
        from src.scheduler import parse_hhmm
        try:
            h, m = parse_hhmm(self.sched_time_var.get())
        except ValueError as exc:
            messagebox.showerror("时间格式错误",
                                 f"{exc}\n请填写 HH:MM，例如 07:00")
            return

        # ★ 2026-09-10：计划任务与 GUI 守护是两套独立抢座机制。计划任务带 WakeToRun
        # 能唤醒睡眠的电脑，GUI 守护只是个 Python 进程 —— 同时开会在同一时刻各抢一次
        # （幂等安全，但多一次浏览器登录、增加风控风险）。这里先问一句，别悄悄重复跑。
        try:
            from src.wake import list_installed_task_names
            _installed = list_installed_task_names()
        except Exception:  # noqa: BLE001
            _installed = []
        if _installed:
            _go = messagebox.askyesno(
                "检测到 Windows 计划任务已安装",
                "这台电脑上已经装了计划任务：\n    "
                + "\n    ".join(_installed)
                + "\n\n它会每天自动唤醒电脑抢座，不需要再开 GUI 守护。\n"
                  "两套同时开会各抢一次（不会重复占座，但多跑一次登录、增加风控风险）。\n"
                  "\n确定还要开启 GUI 守护吗？\n\n"
                  "  选「否」→ 推荐。改用计划任务，电脑可以正常睡眠，\n"
                  "            GUI 只用来改配置，不用点这个按钮。")
            if not _go:
                return
        try:
            prewarm = max(1, int(self.sched_prewarm_var.get()))
        except (TypeError, ValueError):
            prewarm = 2

        # ★ 快照：守护线程要用的值全部在这里取好，不允许它再碰 Tk 变量
        self._sched_cfg = self._build_cfg(dry=False)
        self._sched_keep_awake = bool(self.sched_wake_var.get())
        self._sched_have_sess = False
        self._sched_last_ping = 0.0
        self._sched_last_ui = 0.0

        # ★ 2026-09-07：防休眠没勾时明确告警。实测过一次「取消勾选 → 电脑睡到 07:54 →
        # 睡过头跳过当天」，用户并不知道自己关掉了唯一的保活手段，这里把风险说清楚。
        if not self._sched_keep_awake:
            logger.warning(
                "⚠️ 未启用「等待期间防止系统休眠」：电脑可能在 %02d:%02d 之前进入睡眠，"
                "定时任务不会触发。建议勾选本项，或改用 Windows 计划任务（带唤醒定时器）"
                "—— 详见 scripts/install_windows_task.ps1", h, m)

        # max_late 从配置读（默认 90 分钟）：原硬编码 30 太严，电脑稍微晚醒就跳过当天
        try:
            max_late = max(0, int(self.cfg.get("schedule", {}).get("max_late_minutes", 90)))
        except (TypeError, ValueError, AttributeError):
            max_late = 90

        sched = DailyScheduler(
            open_time=f"{h:02d}:{m:02d}",
            prewarm_minutes=prewarm,
            max_late_minutes=max_late,
            on_prewarm=self._sched_prewarm,
            on_heartbeat=self._sched_heartbeat,
            on_book=self._sched_book,
            on_status=self._sched_on_status,
        )
        self._sched = sched
        self._sched_thread = threading.Thread(
            target=self._run_scheduler, args=(sched,),
            daemon=True, name="ScheduleDaemon")
        self._sched_thread.start()
        self.sched_start_btn.configure(state="disabled")
        self.sched_stop_btn.configure(state="normal")

    def _stop_schedule(self):
        """主线程：请求停止（等待中的任务会立刻中断，正在提交的会跑完）。"""
        if self._sched is not None:
            self._sched.stop()
            logger.info("已请求停止定时守护")
            self.sched_stop_btn.configure(state="disabled")

    def _run_scheduler(self, sched: "DailyScheduler"):
        """守护线程主体：持有 WakeLock 跑到被停止。

        所有 Playwright 动作都通过 `self._worker.submit(...)` 派发到 worker 线程，
        这里只做调度和等待（Playwright sync_api 不能跨线程用 BrowserContext）。
        """
        from src.wake import WakeLock
        lock = WakeLock() if self._sched_keep_awake else None
        self._sched_wake = lock
        try:
            if lock is not None:
                lock.acquire()
            sched.run()
        except Exception as exc:  # noqa: BLE001
            logger.error("定时守护异常: %s", exc, exc_info=True)
        finally:
            if lock is not None:
                lock.release()
            self._sched_wake = None
            # 通知主线程恢复按钮（队列，不直接碰 Tk）
            try:
                self._sched_status_queue.put("@@STOPPED@@")
            except Exception:  # noqa: BLE001
                pass

    def _sched_prewarm(self):
        """预热回调（守护线程）：派发登录到 worker，页面保持不关。"""
        if self._sched_cfg is None:
            return None
        result = self._worker.submit("prewarm", self._sched_cfg, timeout=180.0)
        if result is None or result[0] != "prewarm_ok":
            raise RuntimeError(f"预热登录失败：{result}")
        self._sched_have_sess = True
        return "worker-session"  # 占位：真正的 sess 留在 worker 线程里

    def _sched_heartbeat(self, remaining, phase):
        """等待心跳（守护线程）：每 30 秒探活一次，挂了就重登；顺带刷新倒计时。"""
        now = time.time()
        # 保活探活（已预热阶段才需要）
        if self._sched_have_sess and now - self._sched_last_ping >= 30.0:
            self._sched_last_ping = now
            result = self._worker.submit("ping", None, timeout=20.0)
            if result and result[0] == "pong" and not result[1]:
                logger.warning("预热页面意外失效，立即重新登录保活 ...")
                try:
                    self._worker.submit("prewarm", self._sched_cfg, timeout=180.0)
                except Exception as exc:  # noqa: BLE001
                    logger.error("保活重登失败: %s", exc)
        # 倒计时（节流到 1 秒；★ 只 put 队列，绝不在这里碰 Tk）
        if now - self._sched_last_ui >= 1.0 and self._sched is not None:
            self._sched_last_ui = now
            try:
                self._sched_status_queue.put(self._sched.status_text())
            except Exception:  # noqa: BLE001
                pass
        return None

    def _sched_book(self, _sess):
        """准点提交（守护线程）：复用 worker 里保活的页面。"""
        if self._sched_cfg is None:
            return False
        result = self._worker.submit("book", self._sched_cfg, dry=False,
                                     timeout=240.0)
        ok = bool(result and result[0] == "book_ok" and result[1])
        # 提交完关掉浏览器，别让 Edge 挂一整天
        self._sched_have_sess = False
        try:
            self._worker.submit("close", None, timeout=20.0)
        except Exception:  # noqa: BLE001
            pass
        return ok

    def _sched_on_status(self, text):
        """守护线程 → 主线程的状态转发。

        ★ 这里只是往队列里丢字符串，**绝不能**调用 root.after / StringVar.set：
        Tk 不是线程安全的，后台线程碰它会直接死锁（冒烟测试实测卡死）。
        """
        try:
            self._sched_status_queue.put(text)
        except Exception:  # noqa: BLE001
            pass

    def _sched_set_status(self, text):
        """主线程：更新定时状态标签。"""
        try:
            self.sched_status_var.set(text)
        except Exception:  # noqa: BLE001
            pass

    def _on_schedule_stopped(self):
        """主线程：守护线程退出后恢复按钮。"""
        self._sched = None
        try:
            self.sched_start_btn.configure(state="normal")
            self.sched_stop_btn.configure(state="disabled")
            self._sched_set_status("已停止")
        except Exception:  # noqa: BLE001
            pass

    # --------------------------- 级联事件 ---------------------------
    def _on_campus_change(self, _evt=None):
        """校区切换 → 重置楼层下拉为该校区在 hint 表里的候选，并自动刷新阅览室。"""
        if self._cascading:
            return
        campus = self.vars["campus"].get().strip()
        if not campus:
            return
        floors = list(CAMPUS_FLOOR_HINT.get(campus, ALL_FLOOR_OPTIONS))
        # 保留当前选中（如果还在新候选里）
        cur_floor = self.vars["floor"].get()
        self._cascading = True
        self._set_combo_values("floor", floors)
        if cur_floor in floors:
            self.vars["floor"].set(cur_floor)
        else:
            self.vars["floor"].set(floors[0] if floors else "全部")
        self._cascading = False
        # 自动刷新阅览室
        self.run_thread(self.refresh_rooms)

    def _on_floor_change(self, _evt=None):
        """楼层切换 → 自动刷新阅览室。"""
        if self._cascading:
            return
        self.run_thread(self.refresh_rooms)

    # --------------------------- helpers ---------------------------
    @staticmethod
    def _date_display(value: str) -> str:
        """后端值(today/tomorrow) → 界面显示(今天/明天)；手填日期原样返回。"""
        s = (value or "").strip()
        return DATE_VALUE_TO_DISPLAY.get(s, s)

    @staticmethod
    def _date_value(value: str) -> str:
        """界面显示(今天/明天) → 后端值(today/tomorrow)；手填日期原样返回。"""
        s = (value or "").strip()
        return DATE_DISPLAY_TO_VALUE.get(s, s)

    def _build_cfg(self, dry: bool = False) -> Config:
        data = {k: v.get() for k, v in self.vars.items()}
        data["date"] = self._date_value(data.get("date", ""))
        data["headless"] = self.headless_var.get()
        data["debug"] = self.debug_var.get()
        data["dry_run"] = dry
        return Config(data)

    @staticmethod
    def run_thread(fn, *args, **kwargs):
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    def save_config(self):
        data = {k: v.get() for k, v in self.vars.items()}
        data["date"] = self._date_value(data.get("date", ""))
        data["headless"] = self.headless_var.get()
        data["debug"] = self.debug_var.get()
        # 定时抢座设置一起持久化（下次开程序可自动挂上守护）
        data["schedule"] = {
            "enabled": bool(self.sched_autostart_var.get()),
            "open_time": self.sched_time_var.get().strip() or "07:00",
            "prewarm_minutes": int(self.sched_prewarm_var.get() or 2),
            "keep_awake": bool(self.sched_wake_var.get()),
            # 保留用户已配置的值（默认 90），不要每次保存都硬写回 30
            "max_late_minutes": int(
                (self.cfg.get("schedule") or {}).get("max_late_minutes", 90)),
        }
        # 保留「周二闭馆」规则：GUI 没有对应控件，若不显式回写，
        # 一次保存就会把 config.json 里的 tuesday 段整段抹掉。
        if self.cfg.get("tuesday"):
            data["tuesday"] = dict(self.cfg.get("tuesday"))
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            logger.info("配置已保存到 %s", CONFIG_PATH)
            messagebox.showinfo("保存成功", f"配置已保存到\n{CONFIG_PATH}")
        except Exception as exc:  # noqa: BLE001
            logger.error("保存配置失败: %s", exc)
            messagebox.showerror("保存失败", str(exc))

    # --------------------------- actions ---------------------------
    def do_book(self, dry: bool = False):
        cfg = self._build_cfg(dry=dry)
        logger.info("开始%s预约 ...", "Dry-run " if dry else "真实")
        # 派发任务到 worker 线程（不阻塞 GUI 主线程）
        self.run_thread(self._do_book_async, cfg, dry)

    def _do_book_async(self, cfg: Config, dry: bool):
        """新线程里同步等 worker 完成预约（避免阻塞 GUI 主线程）。

        worker 复用 self._worker._sess（prewarm 留下的 BrowserSession）；
        若 worker 没 sess 或 sess 已死，会先 login_browser 再执行预约。
        """
        t0 = time.time()
        result = self._worker.submit("book", cfg, dry=dry, timeout=180.0)
        elapsed = time.time() - t0
        if result is None:
            logger.error("worker 已停止，本次预约未执行")
            return
        status, payload = result
        if status == "busy":
            logger.warning("worker 正在跑别的事，本次预约未执行")
            return
        if status == "timeout":
            logger.error("worker 预约超时（180 秒）")
            return
        if status == "error":
            logger.error("worker 预约异常: %s", payload)
            return
        ok = payload
        logger.info("预约结果: %s（耗时 %.1f 秒）", "成功" if ok else "失败", elapsed)
        if not dry and not ok:
            self.root.after(0, lambda: messagebox.showwarning(
                "预约未完成",
                "本次预约未成功，请查看上方日志。\n"
                "（若提示「所选时段已有有效预约」或「预约次数已达到每日限制」，是服务器侧原因）"))

    def prewarm(self):
        """提前登录并让 BrowserSession 保活，让随后 do_book 复用 page。

        关键改进（★ 2026-09-05）：旧实现登录完立即关闭 sess（避免跨线程用
        BrowserContext），do_book 时还要再 login_browser → pw.launch + new_context
        浪费 8~12 秒。现在所有 BrowserSession 操作都跑在 worker 线程里，
        prewarm 完成后 BrowserSession 保活、page 不关，do_book 复用同一 page
        直接执行预约，省掉冷启动的 8~12 秒。

        用户体验：
          1) 点「预热登录」 → 浏览器启动 + 登录 + 停在首页
          2) 点「开始预约」 → 直接在那个已登录的 page 上执行选座/滑块/预约
        """
        cfg = self._build_cfg()
        logger.info("预热登录中 ...")
        # 派发任务到 worker（不阻塞 GUI 主线程）
        self.run_thread(self._do_prewarm_async, cfg)

    def _do_prewarm_async(self, cfg: Config):
        """新线程里同步等 worker 完成预热。"""
        t0 = time.time()
        result = self._worker.submit("prewarm", cfg, timeout=60.0)
        elapsed = time.time() - t0
        if result is None:
            logger.error("worker 已停止，本次预热未执行")
            return
        status, payload = result
        if status == "busy":
            logger.warning("worker 正在跑别的事，本次预热未执行")
            return
        if status == "timeout":
            logger.warning("worker 预热超时（60 秒）")
            return
        if status == "error":
            logger.error("预热登录失败: %s", payload)
            return
        # prewarm_ok
        logger.info("预热完成（%.1f 秒）。浏览器已保活停在首页，按「开始预约」将直接执行 — "
                    "通常可省 ~10 秒（避免 pw.launch + new_context）", elapsed)

    def _update_status(self, *_args):
        """底部状态条：实时汇总当前预约目标（含周二闭馆调整后的实际时间窗）。"""
        try:
            def g(k):
                v = self.vars.get(k)
                return (v.get() or "-") if v else "-"

            # 用「实际生效的时间窗」而非界面上的 start/end：
            # 目标日期是周二时会自动换成 08:30~12:00。
            try:
                _c = self._build_cfg()
                s, e = _c.time_window()
                note = _c.time_window_note()
            except Exception:  # noqa: BLE001
                s, e, note = g("start"), g("end"), ""
            self.status_var.set(
                f"目标：{g('campus')} / {g('floor')} / {g('room')} · "
                f"座位 {g('seat')} · {g('date')} {s}~{e}"
                + ("  ⚠ 周二闭馆调整" if note else "")
            )
            if getattr(self, "tuesday_var", None) is not None:
                self.tuesday_var.set(
                    f"⚠ {note}" if note
                    else "周二闭馆日会自动改约 08:30~12:00（当前日期非周二）")
        except Exception:  # noqa: BLE001
            pass

    def refresh_rooms(self):
        from src.session import login_browser
        cfg = self._build_cfg()
        sess = login_browser(cfg)
        try:
            page = sess.page

            # ★ 关键（2026-09-04）：route 拦截必须在 SPA 第一次进 home 之前注册，
            # 否则 SPA 进入时发的 findRoomDuration 接口就拦不到。
            captured, captured_event = self._setup_find_room_duration_route(page)

            self._goto_home(page)
            campuses = self._fetch_campuses(page)
            if campuses:
                self.root.after(0, self._set_combo_values, "campus", campuses)

            # 楼层下拉：以「全部 + 当前校区 hint 楼层」为基准，网站返回的数据只做补充，
            # 保证楼层下拉不会被网站某次的部分返回「瘦身」（之前出现只剩 7层 的现象）。
            floors_web = self._fetch_floors(page)
            base = CAMPUS_FLOOR_HINT.get(cfg.get("campus", ""), ALL_FLOOR_OPTIONS)
            seen = set()
            merged_floors = []
            for f in ["全部"] + list(base) + (floors_web or []):
                if f and f not in seen:
                    seen.add(f)
                    merged_floors.append(f)
            if merged_floors:
                self.root.after(0, self._set_combo_values, "floor", merged_floors)

            # 应用筛选（让 SPA 真正触发 findRoomDurationFun）→
            # _apply_home_filter 已改成「先选全部再选目标」保证触发 watch
            self._goto_home(page)
            self._apply_home_filter(page, campus=cfg.get("campus", ""),
                                     floor=cfg.get("floor", ""))

            # 从 SPA 的响应里过滤（不再用 FLOOR_NAME_MAP 名字猜测）
            rooms = self._collect_rooms_via_api(page, captured=captured,
                                                 captured_event=captured_event,
                                                 floor=cfg.get("floor", ""))

            if not rooms and cfg.get("floor", "") not in ("全部", ""):
                logger.warning("楼层 %s 没有房间（接口响应 pageList 为空），可能是空楼层或筛选未生效",
                               cfg.get("floor", ""))

            self.root.after(0, self._set_combo_rooms, rooms)
            logger.info("刷新到 %d 个阅览室（楼层=%s）: %s",
                        len(rooms), cfg.get("floor", ""), rooms)
        except Exception as exc:  # noqa: BLE001
            logger.error("刷新阅览室失败: %s", exc)
        finally:
            try:
                page.unroute("**/static/frontApi/res/findRoomDuration/**")
            except Exception:
                pass
            sess.close()

    @staticmethod
    def _setup_find_room_duration_route(page):
        """注册 findRoomDuration 接口拦截器，返回 (captured, event)。
        ★ 必须在 SPA 第一次进 home 之前注册。
        """
        import threading
        captured = []
        captured_event = threading.Event()
        first_done = [False]

        def handler(route):
            try:
                resp = route.fetch()
                body = resp.json()
                if isinstance(body, dict) and body.get("status") is True:
                    captured.append({
                        "url": route.request.url,
                        "post": route.request.post_data,
                        "body": body,
                    })
                    if not first_done[0]:
                        first_done[0] = True
                        captured_event.set()
                route.fulfill(response=resp, json=body)
            except Exception:
                route.continue_()

        page.route("**/static/frontApi/res/findRoomDuration/**", handler)
        return captured, captured_event

    @staticmethod
    def _collect_rooms_via_api(page, captured: list, captured_event, floor: str = "") -> list:
        """基于响应拦截器的收集：把已截到的 findRoomDuration 响应按 floorName 过滤。

        ★ 依赖：captured list + event 必须在 SPA 第一次进 home 之前由
          _setup_find_room_duration_route 注册（threading.Event 在收到响应后 set）。
        ★ 关键（2026-09-04）：济大「第N阅览室」不在 N 层（实测第一阅览室在 2 层），
          所以**不能用**名字里的汉字猜楼层，必须用响应的 floorName 字段。
        """
        import time
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if captured_event.is_set():
                break
            page.wait_for_timeout(150)

        # 等全部分页响应都回来（如果 totalCount > pageSize 就翻页）
        last_body = captured[-1]["body"] if captured else None
        total_page = (last_body or {}).get("data", {}).get("totalPage", 1) if last_body else 1
        for _ in range(total_page - 1):
            ok = page.evaluate("""() => {
                const btn = document.querySelector('.el-pagination .btn-next');
                if (!btn || btn.classList.contains('disabled')) return false;
                if (btn.getAttribute('aria-disabled') === 'true') return false;
                btn.click();
                return true;
            }""")
            if not ok:
                break
            page.wait_for_timeout(1200)
            # 等翻页的响应
            cur_count = len(captured)
            t = time.monotonic()
            while time.monotonic() - t < 3.0:
                if len(captured) > cur_count:
                    break
                page.wait_for_timeout(150)

        # 汇总所有 pageList
        all_items = []
        seen = set()
        for c in captured:
            data = c["body"].get("data", {})
            if isinstance(data, dict) and isinstance(data.get("pageList"), list):
                for r in data["pageList"]:
                    k = (r.get("name"), r.get("floorName"))
                    if k not in seen:
                        seen.add(k)
                        all_items.append({
                            "name": r.get("name", ""),
                            "floorName": r.get("floorName", ""),
                            "id": r.get("id", ""),
                        })

        if not all_items:
            return []

        # 按 floorName 精确过滤
        if floor and floor not in ("全部", ""):
            filtered = [r for r in all_items if r["floorName"] == floor]
            skipped = len(all_items) - len(filtered)
            if skipped > 0:
                logger.info("按 floorName=%s 过滤 %d → %d",
                            floor, len(all_items), len(filtered))
            return [r["name"] for r in filtered]
        return [r["name"] for r in all_items]

    @staticmethod
    def _room_list_signature(page) -> str:
        """返回首页房间列表的稳定 hash：所有 .room.el-col 标题排序拼接。

        ★ 2026-09-04 修正：济大 SPA 房间卡片 class 是 .room.el-col，不是 .el-card。
        用 .el-card 永远查不到（=0），导致稳定等待一直超时。
        """
        try:
            titles = page.evaluate(r"""() => {
                return [...document.querySelectorAll('.room.el-col')].map(c => {
                    const t = (c.textContent||'').trim();
                    const m = t.match(/第[^,，。\s]*阅览室[^,，。\s]*/);
                    return m ? m[0] : '';
                }).filter(Boolean).sort();
            }""")
            return "|".join(titles)
        except Exception:
            return ""

    @staticmethod
    def _wait_room_list_stable(page, timeout_ms: int = 6000) -> None:
        """等房间列表稳定：连续 2 轮（间隔 400ms）hash 一致。"""
        import time as _time
        deadline = _time.time() + timeout_ms / 1000.0
        prev = ""
        while _time.time() < deadline:
            cur = App._room_list_signature(page)
            if cur and cur == prev:
                logger.debug("房间列表已稳定 (%d 项)", cur.count("|") + 1 if cur else 0)
                return
            prev = cur
            page.wait_for_timeout(400)
        logger.debug("房间列表稳定等待超时（%.1fs）", timeout_ms / 1000.0)

    @staticmethod
    def _collect_rooms_across_pages(page) -> list:
        """合并所有分页里的阅览室名称。点 el-pagination 的每一页，直到没有下一页。"""
        all_rooms = set()
        max_pages = 8
        for page_idx in range(max_pages):
            rooms = App._fetch_rooms_in_view(page)
            for r in rooms:
                all_rooms.add(r)
            # 看是否有下一页
            has_next = page.evaluate("""() => {
                const btn = document.querySelector('.el-pagination .btn-next');
                if (!btn) return false;
                if (btn.classList.contains('disabled')) return false;
                if (btn.getAttribute('aria-disabled') === 'true') return false;
                return true;
            }""")
            if not has_next:
                break
            page.evaluate("""() => {
                const btn = document.querySelector('.el-pagination .btn-next');
                if (btn) btn.click();
            }""")
            page.wait_for_timeout(1500)  # 等卡片重新渲染
            App._wait_room_list_stable(page, timeout_ms=4000)
        # 翻回第 1 页，避免下次切楼层后第一页 DOM 状态不一致
        try:
            page.evaluate("""() => {
                const pager = document.querySelector('.el-pagination');
                if (!pager) return;
                const first = pager.querySelector('.el-pager li.number');
                if (first && !first.classList.contains('active')) first.click();
            }""")
            page.wait_for_timeout(800)
        except Exception:
            pass
        return sorted(all_rooms)

    def refresh_seats(self):
        from src.session import login_browser
        cfg = self._build_cfg()
        sess = login_browser(cfg)
        try:
            page = sess.page
            self._goto_home(page)
            self._apply_home_filter(page, campus=cfg.get("campus", ""),
                                     floor=cfg.get("floor", ""))
            seats = self._fetch_free_seats(page, cfg.get("room", ""),
                                           cfg.get("date", "tomorrow"))
            self.root.after(0, self._set_combo_values, "seat", seats)
            logger.info("刷新到 %d 个空闲座位: %s", len(seats), seats[:40])
        except Exception as exc:  # noqa: BLE001
            logger.error("刷新座位失败: %s", exc)
        finally:
            sess.close()

    def _set_combo_values(self, key: str, values):
        w = self.combos.get(key)
        if w:
            w["values"] = values
            logger.info("%s 下拉已更新 (%d 项)", key, len(values))

    def _set_combo_rooms(self, values):
        """设置阅览室下拉选项；若当前选中项不在新列表里，自动落到第一项。"""
        w = self.combos.get("room")
        if not w:
            return
        w["values"] = values
        cur = self.vars["room"].get()
        if cur not in values:
            self.vars["room"].set(values[0] if values else "")
        logger.info("room 下拉已更新 (%d 项，当前=%s)", len(values),
                    self.vars["room"].get())

    # --------------------- 联网拉取可选项（读 DOM） ---------------------
    @staticmethod
    def _close_drawer(page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        except Exception:
            pass

    def _goto_home(self, page) -> None:
        """进首页 + 等渲染 + 关抽屉。"""
        page.goto("https://seat.ujn.edu.cn/jsq-v/#/main/home",
                  wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        self._close_drawer(page)

    @staticmethod
    def _select_el_dropdown(page, placeholder: str, option_text: str,
                            keyword: bool = False, timeout_ms: int = 5000) -> bool:
        """根据 placeholder 定位 el-select，触发下拉后选中 option_text。
        el-select 的选项是异步加载的（尤其楼层/校区），这里「轮询等待选项出现 →
        点击 → 回读校验」，避免固定 500ms 等待导致选项还没出来就点不到（卡在楼层）。
        返回是否成功；keyword=True 时用部分匹配。"""
        if not option_text or option_text in ("全部", ""):
            return False
        try:
            opened = page.evaluate("""(ph) => {
                const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
                const inp = inputs.find(i => (i.placeholder||'').trim() === ph);
                if (inp) { inp.click(); return true; }
                return false;
            }""", placeholder)
            if not opened:
                logger.warning("未找到下拉输入框 placeholder=%s", placeholder)
                return False

            deadline = int(time.time() * 1000) + timeout_ms
            while int(time.time() * 1000) < deadline:
                res = page.evaluate("""(args) => {
                    const ph = args.ph, target = args.target;
                    const norm = (s) => (s||'').trim();
                    const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
                    const inp = inputs.find(i => norm(i.placeholder) === ph);
                    const drops = [...document.querySelectorAll('.el-select-dropdown')];
                    const visible = drops.filter(d => {
                        const r = d.getBoundingClientRect();
                        return r.width>0 && r.height>0 && getComputedStyle(d).display!=='none';
                    });
                    const dd = visible[0] || drops[0];
                    if (!dd) return 'no-dropdown';
                    const items = [...dd.querySelectorAll('.el-select-dropdown__item')];
                    let it = items.find(e => norm(e.textContent) === target);
                    if (!it) it = items.find(e => norm(e.textContent).includes(target));
                    if (it) { it.click(); return 'clicked'; }
                    return 'waiting:' + items.length;
                }""", {"ph": placeholder, "target": option_text})
                if res == "clicked":
                    # 回读校验：输入框的值是否已变成目标（避免点到别的/异步未刷新）
                    val = page.evaluate("""(args) => {
                        const ph = args.ph;
                        const norm = (s) => (s||'').trim();
                        const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
                        const inp = inputs.find(i => norm(i.placeholder) === ph);
                        return inp ? (inp.value||'') : '';
                    }""", {"ph": placeholder})
                    if option_text in val or val in option_text or val == option_text:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                        return True
                    # 值未对上，可能点到但渲染慢，再等一轮
                page.wait_for_timeout(400)
            logger.warning("下拉 %s 选 %s 超时（异步选项未就绪）", placeholder, option_text)
            page.keyboard.press("Escape")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("下拉 %s 选 %s 失败: %s", placeholder, option_text, exc)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _apply_home_filter(self, page, campus: str = "", floor: str = "") -> None:
        """在首页应用「校区」与「楼层」筛选。"""
        if campus and campus not in ("主校区", "全部", ""):
            ok = self._select_el_dropdown(page, "场馆选择", campus)
            if ok:
                logger.info("已切校区: %s", campus)
                page.wait_for_timeout(1500)
                self._close_drawer(page)
        if floor and floor not in ("全部", ""):
            ok = self._select_el_dropdown(page, "楼层", floor)
            if ok:
                logger.info("已切楼层: %s", floor)
                page.wait_for_timeout(1500)
                self._close_drawer(page)

    def _read_select_options(self, page, placeholder: str) -> list:
        """触发某 el-select 下拉后，从 DOM 取它当前的所有 option。"""
        try:
            ok = page.evaluate(f"""(placeholder) => {{
                const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
                const inp = inputs.find(i => (i.placeholder||'').trim() === placeholder);
                if (inp) {{ inp.click(); return true; }} return false;
            }}""", placeholder)
            if not ok:
                return []
            page.wait_for_timeout(400)
            opts = page.evaluate("""() => {
                const items = [...document.querySelectorAll('.el-select-dropdown__item')];
                return items.map(e => (e.textContent||'').trim()).filter(Boolean);
            }""")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            return sorted(set(opts))
        except Exception:
            return []

    def _fetch_floors(self, page) -> list:
        self._goto_home(page)
        return self._read_select_options(page, "楼层")

    def _fetch_campuses(self, page) -> list:
        self._goto_home(page)
        return self._read_select_options(page, "场馆选择")

    @staticmethod
    def _fetch_rooms_in_view(page) -> list:
        """当前页视图内读阅览室名称（不导航、不翻页；筛选后房间已在视图中）。"""
        rooms = page.evaluate("""() => {
            const out = new Set();
            document.querySelectorAll('*').forEach(e => {
                const t = (e.textContent || '').trim();
                if (/阅览室/.test(t) && t.length <= 24 && e.children.length <= 2) out.add(t);
            });
            return Array.from(out);
        }""")
        return sorted(r for r in rooms if r)

    def _fetch_rooms(self, page) -> list:
        """从首页读取阅览室名称列表（不应用任何筛选）。"""
        self._goto_home(page)
        return App._fetch_rooms_in_view(page)

    def _fetch_free_seats(self, page, room: str, date_expr: str) -> list:
        """进入某阅览室布局，读取空闲座位号列表。
        视图筛选/导航已由调用方（refresh_seats）完成。"""
        from src.booker import _make_date, _js_root_vm, _js_find_vm

        def _wait_room_visible(name: str, timeout_ms: int = 6000) -> bool:
            if not name:
                return True
            try:
                return page.evaluate(
                    """(name) => {
                        function title(el){
                            const t=(el.textContent||'').trim();
                            return /阅览室/.test(t) && t.length<=24;
                        }
                        const all = [...document.querySelectorAll('*')];
                        return all.some(e => title(e) && e.children.length===0
                                            && (e.textContent||'').trim()===name);
                    }""",
                    name,
                )
            except Exception:
                return False

        clicked = False
        for attempt in range(2):
            if not room:
                clicked = True
                break
            try:
                if not _wait_room_visible(room, timeout_ms=5000):
                    raise RuntimeError(f"首页未渲染出 {room}（楼层切换后视图还在加载）")
                page.get_by_text(room, exact=True).first.click(timeout=6000)
                clicked = True
                break
            except Exception as exc:
                logger.warning("阅览室点击失败(尝试 %d): %s", attempt + 1, exc)
                self._goto_home(page)
                self._apply_home_filter(page, campus=self.vars["campus"].get(),
                                         floor=self.vars["floor"].get())
                page.wait_for_timeout(2000)
                self._close_drawer(page)
        if not clicked:
            raise RuntimeError(f"阅览室 {room} 点击失败，已放弃")
        page.wait_for_timeout(4000)
        self._close_drawer(page)

        make_date = _make_date(date_expr)
        route = page.evaluate(f"""() => {{{_js_root_vm()}{_js_find_vm()}
            const lp = findVM(rootVM(), v => v.seatPreview && v.seatPreview.design);
            const q = lp && lp.$route && lp.$route.query || {{}};
            return {{roomId: q.id, urlDate: q.makeDate}};
        }}""")
        if route.get("roomId") and route.get("urlDate") != make_date:
            page.goto(f"https://seat.ujn.edu.cn/jsq-v/#/main/layout?id={route['roomId']}&makeDate={make_date}",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(4500)
            self._close_drawer(page)

        layout_pred = "v => v.seatPreview && v.seatPreview.design && typeof v.seatPreview.design.getObjects==='function'"
        for _ in range(10):
            ok = page.evaluate(f"""() => {{{_js_root_vm()}{_js_find_vm()} return !!findVM(rootVM(), {layout_pred});}}""")
            if ok:
                break
            page.wait_for_timeout(1000)

        seats = page.evaluate(f"""() => {{{_js_root_vm()}{_js_find_vm()}
            const vm = findVM(rootVM(), {layout_pred});
            if (!vm) return [];
            const objs = vm.seatPreview.design.getObjects().filter(o => o.seat);
            const out = [];
            for (const o of objs) {{
                const s = o.seat;
                if (s.status !== 'FREE') continue;
                const num = s.label!==undefined ? s.label : (s.id!==undefined ? s.id : (s.name!==undefined?s.name:null));
                if (num !== null && num !== undefined) out.push(String(num));
            }}
            return out;
        }}""")
        return sorted(seats, key=lambda x: (len(x), x))


def main():
    root = tk.Tk()
    app = App(root)

    def _on_close():
        # 关 GUI 前先停定时守护（会释放 WakeLock，恢复系统正常电源策略），
        # 再停 worker（worker 会关 BrowserSession，释放 Edge/Playwright 进程）
        try:
            app._stop_schedule()
        except Exception:  # noqa: BLE001
            pass
        try:
            app._worker.stop(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
