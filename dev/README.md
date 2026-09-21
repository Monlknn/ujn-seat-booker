# dev — 开发期存档脚本

这里保存的是项目**探索与调试阶段**用过的一次性脚本，**不是**运行 `ujn-seat-booker` 所需的代码。
正式代码在 [`../src/`](../src)，回归测试在 [`../debug/`](../debug)。

## dev/probes/ — 接口与页面探测

| 脚本 | 用途 |
|---|---|
| `probe_auth.py` / `probe_browser.py` | SSO 登录流程与 token 换取探测 |
| `probe_api.py` / `probe_seat.py` / `probe_flow.py` | 接口与选座交互探测 |
| `probe_floor*.py` / `probe_campuses.py` | 阅览室 / 楼层 / 校区分页结构探测 |
| `probe_home_dom.py` / `probe_rescheck*.py` / `probe_my_reservations.py` | 首页 DOM 与「我的预约」结构探测 |
| `probe_slider.py` / `probe_drag_map.py` / `probe_drag_solution.py` | 滑块验证码缺口定位与拖动轨迹实验 |
| `probe_js_*.js` | 注入页面的 JS 片段（配 `node --check` 做语法校验） |
| `fetch_captchas.py` / `capture_tac_request.py` | 抓取验证码样本与原始请求 |
| `extract*.py` | 从 `debug/app.js.txt` 反查前端接口与字段 |
| `explore_browser.py` / `debug_scores.py` / `diag_gap.py` | 浏览器探索、匹配分数统计、缺口诊断 |

## dev/legacy_tests/ — 早期单测

滑块匹配算法的早期验证脚本，已被 [`../debug/verify_*.py`](../debug) 取代。

## 运行注意

- 多数脚本以**仓库根**为工作目录：`cd <repo> && python dev/probes/xxx.py`
- 脚本内的仓库根定位已同步为 `Path(__file__).resolve().parents[2]`
- 部分脚本依赖 `debug/` 下的历史抓包样本（如 `app.js.txt`、`tac_live_bg.png`），**这些样本未入库**
- 因此这些脚本主要用于**留存开发轨迹与思路**，不保证可直接复跑
