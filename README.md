# 济南大学图书馆座位自动预约（ujn-seat-booker）

仿 [辽宁大学 LNU-LibSeat-Automation](https://github.com/xunrana/LNU-LibSeat-Automation)
改造而来，用于济南大学 `seat.ujn.edu.cn` 的座位预约系统。

## 与辽大项目的关键差异

| 项目 | 辽宁大学 (LNU) | 济南大学 (UJN) |
| --- | --- | --- |
| 登录 | 网页端账号+密码 | 统一身份认证 SSO（CAS 风格 tpass，**无需验证码**） |
| 验证码 | 文字点选验证码（YOLOv8+Siamese 识别） | **TAC 滑块拼图验证码**（出现在预约提交环节） |
| 预约方式 | Selenium 操盘网页 DOM | 全程真实浏览器（Edge / Playwright）+ 前端 Vue VM / canvas 交互 |
| 默认架构 | 桌面 GUI + 多账号并发 | 命令行脚本 + 定时守护 |

> 已落实的关键信息：**登录不需要验证码，滑块验证码在预约提交（点"预约"后）才出现。**

---

## 实际工作流程（browser 模式，默认）

济大座位系统是 **Vue 2 + Element-UI + fabric.js** 单页应用，前端代码在
`https://seat.ujn.edu.cn/jsq-v/`，后端接口基址为
`https://seat.ujn.edu.cn/jsq/static/frontApi/`，登录态 token 存放在
`sessionStorage` 的 `jsq_p-token`（不是 `token`）。`_book_via_browser()` 的完整流程：

1. **SSO 登录**：打开 `sso.ujn.edu.cn` 的 CAS 登录页，用页面自带的 `strEnc`
   生成 `rsa = 3DES(strEnc(账号+密码+lt, '1','2','3'))`，填充隐藏字段
   （`rsa`/`ul`/`pl`/`lt`/`execution`/`_eventId`）后直接 `loginForm.submit()`。
   全程**无验证码**，纯账号密码即可。登录成功后跳回 `seat.ujn.edu.cn` 已登录态。
2. **进入首页并关闭"系统公告"遮罩**（`Escape`）。
3. **按校区 + 楼层筛选**：`config.json` 里配的 `campus` / `floor` 会先驱动对应的
   `el-select` 下拉，把目标阅览室所在楼层的所有阅览室折叠成一页
   （默认视图是「全部楼层分 3 页」的第 1 页，常导致 `第七阅览室中区` 这种楼上的
   房间找不到）。选 `7层` 后该层所有「第七/第八阅览室」同页出现，无需翻页。
4. **点击目标阅览室** → 进入座位布局页（`#/main/layout?id=...&makeDate=...`）。
5. **读取座位布局**：通过 Vue 根 VM → 找到 `seatPreview.design`（fabric.js canvas），
   遍历 `getObjects()` 中带 `seat` 的对象，按 `label`/`id`/`name` 匹配目标座位，
   计算其中心点在屏幕 canvas 上的像素坐标并 `page.mouse.click()` 选中。
6. **等待预约弹窗打开**（遍历 VM 找含 `codeCheck`/`confirmFilter` 的那个），
   设置起止时间（`search.starTimeValue`/`search.endTimeValue`）。
7. **判断是否要滑块**：读 `sessionStorage.systemInfo.mackCaptcha`。
   若为 true，调用 `vm.codeCheck()` 触发 TAC 滑块渲染，再调用 `slider.solve()` 解滑块。
8. **提交**：滑块通过后前端发起 `frontApi` 的 `freeBook` 请求
   （URL 形如 `.../freeBook/{seatId}/{date}/{startMin}/{endMin}?capToken=SLIDER_xxx`），
   轮询 `vm.orderSuccess` 直到成功。

---

## 三种运行模式

| 模式 | 状态 | 说明 |
| --- | --- | --- |
| `browser` | **默认、可用** | 全程 Playwright + Edge 真实浏览器，覆盖 SSO 登录、canvas 选座、TAC 滑块、提交。 |
| `api` | 历史兼容、可能已失效 | 直连 `/rest/v2/*` 接口（`/rest/auth` 登录现多返回 `code:31 验证码错误`，因此登录通常走不通）。 |
| `auto` | 不稳定 | 先试 `api`，失败再降级 `browser`。因 `api` 登录多已失效，实际几乎总是落到 `browser`。 |

> 结论：**直接用 `browser` 模式**。`api`/`auto` 保留仅为历史兼容，不要依赖其登录链路。

---

## 目录结构

```
ujn-seat-booker/
├── src/                   核心代码
│   ├── cli.py             命令行入口（once / serve / gui / rooms / seats / validate-config / wake）
│   ├── gui.py             Tkinter 图形界面
│   ├── config.py          配置加载、目标日期解析、时间窗单一解析器（含周二特例）
│   ├── session.py         SSO 登录与浏览器会话（会话缓存复用）
│   ├── booker.py          抢座主流程：选座 → 解滑块 → 提交 → 结果核对
│   ├── slider.py          滑块验证码缺口定位与类人拖动
│   ├── scheduler.py       到点调度（迟到容忍窗口内补跑）
│   ├── wake.py            防休眠 WakeLock
│   ├── api.py             接口封装
│   └── logger.py          日志
├── scripts/               install_windows_task.ps1（Windows 计划任务安装 / 卸载）
├── debug/                 回归测试（verify_*.py）与接口 / 页面结构样本
├── dev/                   开发期存档脚本（探测、早期单测）— 非运行所需，见 dev/README.md
├── config.example.json    配置模板（复制为 config.json 后填写）
├── requirements.txt
└── README.md
```

> `config.json`、`.session/`、`logs/` 含个人凭据与运行时数据，已在 `.gitignore` 中排除，不入库。

## 安装

```bash
cd ujn-seat-booker
pip install -r requirements.txt
```

浏览器用本机已安装的 **Microsoft Edge**（Chromium 内核），通过 Playwright 的
`channel="msedge"` 启动，**无需额外下载 Chromium**。前提是本机已安装 Edge。

如需用 Chrome 或 Chromium 代替，改 `config.json` 的 `browser_channel`：

```json
"browser_channel": "chrome"        // 需本机装 Chrome
// 或 "chromium" 并先执行：playwright install chromium
```

依赖：`requests`（仅 api 模式）、`playwright`、`opencv-python`、`numpy`、`Pillow`。

> 建议使用隔离环境避免污染系统 Python：
> ```bash
> python -m venv .venv && .venv\Scripts\activate
> pip install -r requirements.txt
> ```

---

## 配置

复制模板并填写：

```bash
cp config.example.json config.json
```

`config.json` 字段说明：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `username` | 学号 | — |
| `password` | 密码 | — |
| `campus` | **校区下拉选项**（`场馆选择`）：`主校区` / `舜耕校区` / `多功能区（主校区）`；默认 `主校区` | `主校区` |
| `floor` | **楼层下拉选项**：`全部` / `2层`～`7层`；切到某层后该层所有阅览室会被折叠到一页，避免"只能约第 1 页" | `全部` |
| `room` | 阅览室名（如 `第七阅览室中区`） | `第一阅览室` |
| `seat` | 座位号 | `001` |
| `date` | `tomorrow` / `today` / `YYYY-MM-DD` | `tomorrow` |
| `start` / `end` | 起止时间 `HH:MM` | `08:00` / `12:00` |
| `tuesday` | **周二闭馆特例**：目标日期是周一时段自动切换，见下 | `{enabled:true, start:"08:30", end:"12:00"}` |
| `mode` | `browser` / `api` / `auto` | `browser` |
| `headless` | 浏览器是否无头运行 | `true` |
| `browser_channel` | `msedge` / `chrome` / `chromium` | `msedge` |
| `max_retry` | 滑块重试次数 | `5` |
| `slide` | 滑块 DOM 选择器候选 + `tolerance` | 见下 |
| `debug` | 调试模式（控制台 DEBUG + 保存截图到 `debug/`） | `false` |
| `dry_run` | 拦截 `freeBook`，不提交真实预约（也可命令行 `--dry-run`） | `false` |

### 周二闭馆特例（`tuesday`）

图书馆**每周二 12:00~16:00 闭馆**，无法按 `08:20~22:00` 连约整天。脚本会在下单前检测
**预约目标日期**（即 `date` 解析后的那天，不一定是"今天"）是否为周二：

| 目标日期 | 实际提交时段 |
| --- | --- |
| 非周二 | `start` ~ `end`（如 `08:20~22:00`） |
| **周二** | `tuesday.start` ~ `tuesday.end`（默认 `08:30~12:00`） |

```json
"tuesday": { "enabled": true, "start": "08:30", "end": "12:00" }
```

- 不想要这个行为 → `"enabled": false`（周二也按 `start`/`end` 提交）。
- 时段判定统一走 `Config.time_window()`，路由拦截、幂等预检、弹窗设值、后台铁证核对
  全部使用**同一个调整后的时间窗**，不会出现"提交 08:30 却按 08:20 核对"。
- 生效时日志会打印：`时间窗调整：<日期> 是周二，图书馆 12:00~16:00 闭馆 → 本次改为预约 08:30~12:00`。
- GUI 的「开始/结束时间」下方会实时提示；`validate-config` 也会打印实际生效时段。

`slide` 关键子项（按实际页面微调）：

```json
"slide": {
  "slider_candidates": [".slider-move-btn", ...],   // 拖动按钮
  "bg_candidates":     ["#tianai-captcha-slider-bg-img", ...],  // 背景图
  "piece_candidates":  [".slider-img-div img", ...],            // 拼图块
  "tolerance": 0       // 缺口检测已较准确，拖动到计算位置即可
}
```

---

## 使用

```bash
# 校验配置是否填写完整
python -m src.cli validate-config

# 预约一次（全程浏览器，真实提交！）
python -m src.cli once

# 调试：不提交真实预约，仅验证"登录→选座→解滑块→freeBook 被拦截"
python -m src.cli once --dry-run

# 调试：开启 verbose 日志 + 保存截图
python -m src.cli once --debug

# 定时守护：每天 07:00 自动预约次日座位（Ctrl+C 退出）
python -m src.cli serve 07:00
```

> ⚠️ `once` / `serve` 会发起**真实预约**。务必确认预约后能按时签到，否则累计失约会进黑名单。
> 调试滑块或验证流程时，请使用 `--dry-run`，它会在浏览器层拦截 `freeBook` 请求，
> 日志会打印 `[dry-run] 滑块验证已通过，freeBook 已被拦截，未产生真实预约`。

---

## 定时抢座（每天 07:00 自动约）

济大约 **07:00 开放次日预约**。抢座拼的是"开放那一刻谁先提交"，而 SSO 登录本身
要 8~10 秒 —— 等 07:00 再开始登录就晚了。所以流程设计成：

```
06:58:00  提前 N 分钟启动 → 登录 → 把页面停在首页（房间卡片已渲染）
          ↓  页面保活，不关闭浏览器
07:00:00  准点：直接用已登录的页面选座 → 解滑块 → 提交
          ↓
          提交完立即关闭浏览器、释放防休眠锁，电脑恢复可睡眠
```

### 三个关键设计

| 问题 | 做法 |
| --- | --- |
| **登录耗时挤占抢座窗口** | 提前 1~2 分钟登录并**保活页面**，准点直接提交（省约 9~10 秒冷启动） |
| **电脑一睡就错过** | 等待期间用 `SetThreadExecutionState` 阻止系统睡眠；配合计划任务的**唤醒定时器**，电脑平时照常睡，到点自动醒 |
| **`time.sleep(24h)` 会漂移** | 改成**分片等待**：每 0.05~20 秒重新对齐墙上时钟（越接近目标醒得越勤）。睡醒后立刻发现"已过点"并补跑；迟到超过 30 分钟则判定睡过头、跳过当天，不在奇怪的时间乱约 |

### 命令行

```bash
# 每天 07:00 抢座，提前 2 分钟预热登录（默认参数，常驻循环）
python -m src.cli serve 07:00

# 提前 1 分钟预热
python -m src.cli serve 07:00 --prewarm 1

# 只跑今天这一次，跑完退出（配合 Windows 计划任务用这个）
python -m src.cli serve 07:00 --once

# 不阻止系统睡眠（默认阻止；用电池且必须合盖休眠时才关）
python -m src.cli serve 07:00 --no-wake-lock

# 自检防休眠是否生效：持有锁 30 秒，期间系统不会睡
python -m src.cli wake 30

# 先用 dry-run 验证链路（不会真实占座）
python -m src.cli serve 07:00 --dry-run --once
```

### GUI

界面底部「⏰ 定时抢座」卡片：填抢座时刻（默认 `07:00`）、提前预热分钟（默认 2）、
勾选阻止睡眠，点「启动定时守护」即可挂机。状态栏实时显示倒计时
（`等待预热｜2 小时 13 分后`）。

勾上「打开本程序时自动挂上守护」并点「保存配置」，下次开 GUI 会自动开始等待 ——
适合把它当常驻小工具留在后台。

> ⚠️ 守护线程用的是**启动那一刻的配置快照**（Tk 变量不能跨线程读）。
> 改了阅览室/座位/时段后，需要「停止 → 启动」一次守护才生效。

### Windows：让电脑到点自己醒（推荐这样挂机）

不需要整晚不关机。注册一个带**唤醒定时器**的计划任务，电脑平时正常睡觉，
到点被唤醒执行，跑完继续睡：

```powershell
# 安装（默认每天 07:00 抢座，提前 2 分钟唤醒启动）
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1

# 自定义时刻
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Time 07:00 -Prewarm 2

# 额外加一个「登录时启动 GUI 常驻守护」的任务（适合白天一直开机）
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -GuiOnLogon

# 卸载
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Uninstall
```

配套检查项：

1. **唤醒定时器只对「睡眠 / 休眠」有效，关机叫不醒。** 晚上别关机，让它睡眠即可。
2. 笔记本合盖：控制面板 → 电源选项 → 选择关闭盖子的功能 → 「关闭盖子时」设为**睡眠**。
3. 排查命令：
   ```powershell
   schtasks /query /tn "UJN-Seat-Booker" /fo LIST /v   # 看任务状态
   powercfg /waketimers                                # 看已注册的唤醒定时器
   powercfg /lastwake                                  # 看上次是谁唤醒的
   schtasks /run /tn "UJN-Seat-Booker"                 # 立即手动跑一次
   ```

---

## 图形界面（GUI，推荐）

不想手改 `config.json`？用桌面 GUI 点选即可（类似辽大的桌面程序，**零额外依赖**，仅用 Python 自带的 Tkinter）：

```bash
# 用带 Tkinter 的 Python 运行（Windows 自带 Python 即可；需已装 playwright/opencv-python/numpy）
python -m src.cli gui
# 或
python -m src.gui
```

界面功能：
- 学号 / 密码 / **校区（下拉）** / **楼层（下拉）** / 阅览室 / 座位 / 日期 / 起止时间 / 模式 / 浏览器通道 均可下拉或输入选择。
- 「刷新阅览室」：先按官网 DOM 实际值回填校区（场馆选择）和楼层（2层...7层）下拉，再按你当前选择的校区 + 楼层筛选后读取该层所有阅览室。
- 「刷新空闲座位」：先按你的校区/楼层筛选 + 选阅览室，再读 fabric.js canvas 的 `FREE` 座位列表。
- 「开始预约(真实)」一键真实下单；「Dry-run 测试」只跑流程、不真正占座（拦截 freeBook）。
- 「保存配置」把当前选项写回 `config.json`。
- 底部实时日志，便于排查。

> 运行 GUI 的 Python 必须带 Tkinter（标准 Windows Python 自带）；同时需要
> `playwright` / `opencv-python` / `numpy` / `pillow` / `requests`（见 `requirements.txt`）。
> 本机标准 Python 已齐备，直接 `python -m src.cli gui` 即可。
>
> ⚠️ **不要用 WorkBuddy 托管 venv 跑 GUI**：该 venv 未安装 Tkinter，会报
> `ModuleNotFoundError: No module named 'tkinter'`。若 `python` 指向托管 venv，请改用系统 Python：
> `C:/Users/lenovo/AppData/Local/Microsoft/WindowsApps/python3.exe -m src.cli gui`

---

## TAC 滑块验证码原理

济大提交环节用 **tianai-captcha (TAC) 滑块拼图**。与辽大"文字点选+YOLOv8"完全不同，
这里用 **计算机视觉 + 类人拖动**：

### 1. 图片提取（直接从已渲染 DOM）
TAC 渲染后 DOM 结构（`#show-code-check-wrap` 内）：
- 背景图 `#tianai-captcha-slider-bg-img`：native 600×360，屏幕显示 300×180（**缩放 0.5**）。
- 拼图块 `.slider-img-div img`：native 110×360，屏幕显示 55×180。
- 拖动按钮 `.slider-move-btn`：约 63×45。

代码用 `naturalWidth` 区分（背景 580–620 且 jpeg；拼图 100–120 且 png）以避免抓错图。

### 2. 缺口定位（边界检测器，主策略）
TAC 的"缺口"不是简单暗洞/亮洞，而是带 3D 阴影的凸起，**强度 NCC 容易失效**。
`slider._match_offset_boundary` 改用"边界特征"：

1. 用拼图 alpha 掩膜生成 `inner`（腐蚀）/ `ring_outer`（膨胀-掩膜）/ `boundary`（膨胀-腐蚀）。
2. 对背景做 Sobel 梯度幅值 `grad`。
3. 对每个候选 x：计算"缺口内 vs 缺口外"的 RGB 对比 `contrast`、缺口内方差 `in_std`、
   边界处梯度均值 `grad_mean`，得 `coarse[x] = grad_mean * contrast / (1+in_std)`。
4. **粗峰 + 精修**：取 `coarse` 峰值，在 ±15px 内用 `coarse*edge` 二次定位得到 `gap_x` 与 `margin`。

评分足够（`score>=20`）或 NCC 回退 `margin>=1.15` 即采信；几何边界
`10 <= gap_x <= bgW - pieceW - 10` 仍为硬约束。该检测器在猫/小鸡/鹰/六边形/鹿等多种
拼图形状下均验证有效（正确峰 score 1e4~1e5 量级）。

### 3. 拖动几何
- **缩放**：`scale = 屏幕滑轨宽度 / 背景 native 宽度 = 0.5`（300/600）。
- **映射**：拖动按钮位移与拼图块移动 **1:1**（实测 drag 80px → 拼图移动 80px）。
  因此 `drag_dx = int(gap_x * scale)`。

### 4. 类人拖动 `_drag`
35–55 步缓动轨迹 + **过冲 3~8px（70% 概率）再回拉** + 步间随机延迟
（两端 12–30ms、中段 5–18ms、12% 概率额外 40–90ms 停顿）+ 垂直抖动 ±2.5px +
释放前小幅晃动。目的是让 TAC 服务端的人机校验通过（纯匀速直线拖动容易被拒）。

`slider.solve()` 最多尝试 3 次，结果不可信时调用 `_reload_tac()` 刷新验证码后重试。

---

## 浏览器模式适配说明（重要）

`_book_via_browser()` 目前已完成 **SSO 登录 + canvas 选座 + TAC 滑块求解 + 提交**
整条链路，并在 `--dry-run` 下验证通过（滑块被服务端接受、`freeBook` 被拦截）。
若后续济大前端改版导致某个选择器失效，请在 `config.json` 的 `slide` 候选列表，
或 `booker.py` 的 VM 查找逻辑中按 DevTools 实测结果更新即可。

---

## 免责声明

本工具仅供学习交流与个人自动化使用。请遵守《济南大学图书馆座位预约系统管理规定》：
预约后按时签到（允许提前 45 分钟签到，超开始时间 15 分钟未签到视为违约）；
失约累计会进入黑名单（7~30 天），请勿滥用，调试时优先使用 `--dry-run`。
