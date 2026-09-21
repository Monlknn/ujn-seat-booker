<#
.SYNOPSIS
  济南大学座位预约 —— Windows 定时唤醒抢座任务（安装 / 卸载）

.DESCRIPTION
  创建一个计划任务：每天在「抢座时刻 - 提前分钟」这个时间点把电脑从睡眠中唤醒，
  然后执行：

      唤醒 → 启动脚本 → 登录预热（保活页面）→ 等准点 → 提交 → 退出

  脚本一启动就申请「阻止系统睡眠」，提交完成后立即释放，系统恢复正常的省电策略。
  所以电脑平时可以照常睡觉，不需要整晚不关机 —— 这正是辽大抢座脚本的思路。

  注意：唤醒定时器只对「睡眠 / 休眠」有效。如果电脑是「关机」，到点不会自己开机
  （那是 BIOS 的 RTC 唤醒才能做的事）。想万无一失就别关机，保持睡眠状态。

.PARAMETER Time
  抢座时刻 HH:MM，默认 07:00（济大约 07:00 开放次日预约）

.PARAMETER Prewarm
  提前几分钟启动并预热登录，默认 2（SSO 登录约 8~10 秒，1~2 分钟足够）

.PARAMETER Uninstall
  删除已创建的任务

.PARAMETER GuiOnLogon
  额外创建一个「用户登录时启动 GUI 常驻守护」的任务（适合白天一直开机的场景）

.PARAMETER BackupMinutes
  补跑冗余：在抢座时刻之后额外注册几个「补跑」任务（逗号分隔的分钟数）。
  默认 "5,15" —— 即 07:05 和 07:15 各再跑一次。
  为什么需要：唤醒定时器偶尔会失败（BIOS/驱动/电源策略问题），主任务没起来时
  补跑能兜住。重复跑是安全的 —— 济大对「同座同时段已有预约」返回
  {status:False, message:"已有有效预约", data:{ctId:...}}，脚本第 6 层兜底判成功，不会重复占座。
  传 "" 表示不注册补跑。

.PARAMETER TaskName
  计划任务名，默认 UJN-Seat-Booker

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Time 07:00 -Prewarm 2
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -BackupMinutes "5,15,30"
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -GuiOnLogon
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Uninstall
#>
param(
    [string]$Time = "07:00",
    [int]$Prewarm = 2,
    [string]$BackupMinutes = "5,15",
    [switch]$Uninstall,
    [switch]$GuiOnLogon,
    [string]$TaskName = "UJN-Seat-Booker",
    [string]$PythonExe = "",
    [string]$GuiPythonExe = "",
    [string]$ProjectDir = ""
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- 路径推断
if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent $PSScriptRoot   # scripts\ 的上级 = 项目根
}

function Find-Python {
    param([string[]]$Candidates)
    foreach ($c in $Candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command python3 -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

if (-not $PythonExe) {
    # 优先用托管 venv（装了 playwright / cv2 / numpy）
    $PythonExe = Find-Python @(
        "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe",
        "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\bin\python.exe"
    )
}
if (-not $PythonExe) {
    Write-Host "[x] 没找到 python.exe，请用 -PythonExe 显式指定。" -ForegroundColor Red
    exit 1
}

# GUI 用系统 Python（托管 venv 通常没装 tkinter）
if (-not $GuiPythonExe) {
    $GuiPythonExe = Find-Python @(
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\python3.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "C:\Python313\pythonw.exe"
    )
    if (-not $GuiPythonExe) { $GuiPythonExe = $PythonExe }
}

# ---------------------------------------------------------------- 卸载
if ($Uninstall) {
    # 主任务 + GUI + 所有补跑任务（BK1/BK2/...）一起清掉
    $names = @($TaskName, "$TaskName-GUI")
    foreach ($i in 1..9) { $names += "$TaskName-BK$i" }
    foreach ($n in $names) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($t) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "[-] 已删除计划任务: $n" -ForegroundColor Yellow
        }
    }
    Write-Host "完成。可用 schtasks /query /fo LIST 确认。"
    exit 0
}

# ---------------------------------------------------------------- 校验
try {
    $target = [datetime]::ParseExact($Time, "HH:mm", $null)
} catch {
    Write-Host "[x] 时间格式应为 HH:MM，例如 07:00" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $ProjectDir "src\cli.py"))) {
    Write-Host "[x] 项目目录不对（找不到 src\cli.py）: $ProjectDir" -ForegroundColor Red
    exit 1
}

$startAt = $target.AddMinutes(-$Prewarm)
$startStr = $startAt.ToString("HH:mm")

Write-Host "=========================================================="
Write-Host " 济南大学座位预约 · 定时唤醒抢座"
Write-Host "=========================================================="
Write-Host " 项目目录   : $ProjectDir"
Write-Host " Python     : $PythonExe"
Write-Host " 抢座时刻   : $Time  （提前 $Prewarm 分钟启动预热）"
Write-Host " 任务启动   : 每天 $startStr"
Write-Host "=========================================================="

# ---------------------------------------------------------------- 打开唤醒定时器
# Windows 默认允许，但有些机器/组策略会禁掉，这里显式打开。
# 注意：powercfg /waketimers 只能查询不能 enable，需改电源计划的
# 「允许唤醒定时器」(rtcwake)，该操作需要管理员权限，无权限时跳过并提示。
try {
    & powercfg /setacvalueindex scheme_current sub_sleep rtcwake 1 2>&1 | Out-Null
    & powercfg /setdcvalueindex scheme_current sub_sleep rtcwake 1 2>&1 | Out-Null
    & powercfg /setactive scheme_current 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[i] 已确保系统唤醒定时器开启（允许唤醒定时器 = 启用）"
    } else {
        Write-Host "[!] 无管理员权限，跳过电源计划修改。若「允许唤醒定时器」被禁用，" -ForegroundColor Yellow
        Write-Host "    请以管理员身份重跑本脚本，或在电源选项中手动启用。" -ForegroundColor Yellow
    }
} catch {
    Write-Host "[!] 电源计划修改失败，请手动检查「允许唤醒定时器」设置。" -ForegroundColor Yellow
}

# ------------------------------------------------- ★ 唤醒后不要求密码（2026-09-10 关键修复）
# 实测踩坑：Register-ScheduledTask 默认 LogonType=Interactive（只在用户登录时运行）。
# 电脑被 WakeToRun 唤醒后停在【锁屏界面】，此时还没有用户登录会话 → 任务不触发，
# 只能等用户白天开机登录后靠 StartWhenAvailable 补跑（实测推迟到当天晚上 22:23）。
# 关掉「唤醒需要密码」→ 唤醒后直接回到桌面 → Interactive 任务才能准时跑。
# 注：Playwright 必须启动真实浏览器，需要交互式桌面会话，所以不能改 LogonType=S4U
#（S4U 在会话 0 里没有桌面，浏览器起不来，会报 0xC0000142）。
try {
    & powercfg /setacvalueindex scheme_current sub_none consolelock 0 2>&1 | Out-Null
    & powercfg /setdcvalueindex scheme_current sub_none consolelock 0 2>&1 | Out-Null
    & powercfg /setactive scheme_current 2>&1 | Out-Null
    Write-Host "[i] 已设置：从睡眠唤醒后【不需要】输入密码（否则锁屏界面下任务不会触发）" -ForegroundColor Yellow
} catch {
    Write-Host "[!] 无法自动关闭唤醒密码，请手动设置：" -ForegroundColor Yellow
    Write-Host "    设置 → 账户 → 登录选项 → 需要登录 → 改为【从不】" -ForegroundColor Yellow
}

# ------------------------------------------------- ★ 插电时永不待机（2026-09-11 关键修复）
# 实测踩坑（09-11 早上没约上）：
#   本次事故根因不是锁屏，而是**机型只有 S0 低电量待机（Modern Standby），没有 S3 / 休眠**。
#   `powercfg /a` 显示：S1/S2/S3 均「系统固件不支持」，休眠「尚未启用」。
#   S0 待机下，计划任务的 WakeToRun 唤醒定时器**不生效** —— 
#   `powercfg /lastwake` 显示「唤醒历史记录计数 0」，即 06:58 的唤醒请求被直接忽略，
#   电脑一直处于 S0 待机，直到用户 07:10 手动唤醒，任务才靠 StartWhenAvailable 补跑
#   （三个任务的 LastRunTime 都是 07:10）。
#
# 既然这台机器「叫不醒」，那就干脆**别让它睡**：
#   插电时把「在此时间后睡眠」设为 0（从不），电脑整晚保持运行，计划任务准点触发。
#   代价：笔记本整晚约 10~20W（一夜不到 0.2 度电）。若想恢复省电，把本段注释掉重跑即可。
#   注：DC（电池）不设 —— 电池时仍正常省电，但**不插电就没法保证准点抢座**。
try {
    & powercfg /setacvalueindex scheme_current sub_sleep standbyidle 0 2>&1 | Out-Null
    & powercfg /setacvalueindex scheme_current sub_sleep hibernateidle 0 2>&1 | Out-Null
    # 笔记本合盖不动作（台式机没有 lid，静默忽略；不加这行笔记本一合盖就进 S0 待机）
    & powercfg /setacvalueindex scheme_current sub_buttons lidaction 0 2>&1 | Out-Null
    & powercfg /setactive scheme_current 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[i] 已设置：插电时【永不休眠/待机】+ 合盖不操作" -ForegroundColor Yellow
        Write-Host "    （S0 机型唤醒定时器不可靠，只能靠『不睡』保证准点；请保持插电）" -ForegroundColor Yellow
    } else {
        Write-Host "[!] 无管理员权限，跳过『永不待机』设置。" -ForegroundColor Red
        Write-Host "    请以管理员身份重跑本脚本，否则电脑待机后任务不会触发。" -ForegroundColor Red
    }
} catch {
    Write-Host "[!] 待机设置失败，请在电源选项中手动把「睡眠」设为「从不」。" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 任务 A：定时唤醒抢座
$arg = "-m src.cli serve $Time --prewarm $Prewarm --once"
$action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument $arg -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Daily -At $startStr
# WakeToRun：允许从睡眠中唤醒执行；StartWhenAvailable：错过了就找机会补跑
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "[+] 已创建计划任务: $TaskName"

# ---------------------------------------------------------------- 补跑冗余任务
# 唤醒定时器偶尔会失效（BIOS / 驱动 / 电源策略），主任务没起来时这些补跑能兜住。
# 重复跑是幂等的：已有预约时服务端返回 "已有有效预约" + ctId，脚本判成功，不会重复占座。
$bkMinutes = @()
if ($BackupMinutes -and $BackupMinutes.Trim() -ne "") {
    $bkMinutes = $BackupMinutes -split "," | ForEach-Object { $_.Trim() } |
                 Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ }
}
$bkIdx = 0
foreach ($bk in $bkMinutes) {
    $bkIdx++
    $bkAt = $target.AddMinutes($bk)
    $bkStr = $bkAt.ToString("HH:mm")
    $bkName = "$TaskName-BK$bkIdx"
    $bkTrigger = New-ScheduledTaskTrigger -Daily -At $bkStr
    Register-ScheduledTask -TaskName $bkName `
        -Action $action -Trigger $bkTrigger -Settings $settings -Force | Out-Null
    Write-Host "[+] 已创建补跑任务: $bkName (每天 $bkStr，抢座时刻后 $bk 分钟)"
}
if ($bkIdx -eq 0) {
    Write-Host "[i] 未注册补跑任务（-BackupMinutes 为空）。建议至少留 1~2 个兜底。"
}

# ---------------------------------------------------------------- 任务 B：登录时启动 GUI（可选）
if ($GuiOnLogon) {
    $a2 = New-ScheduledTaskAction -Execute $GuiPythonExe `
        -Argument "-m src.cli gui" -WorkingDirectory $ProjectDir
    $t2 = New-ScheduledTaskTrigger -AtLogOn
    $s2 = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Days 365) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "$TaskName-GUI" `
        -Action $a2 -Trigger $t2 -Settings $s2 -Force | Out-Null
    Write-Host "[+] 已创建计划任务: $TaskName-GUI (登录时启动 GUI 常驻守护)"
}

# ---------------------------------------------------------------- 提示
Write-Host ""
Write-Host "----------------------------------------------------------" -ForegroundColor Cyan
Write-Host " 装好了。几点说明：" -ForegroundColor Cyan
Write-Host "----------------------------------------------------------" -ForegroundColor Cyan
Write-Host " 1. 唤醒定时器只对【睡眠/休眠】有效，关机是叫不醒的。"
Write-Host "    想万无一失：晚上别关机，让它睡眠就行（合盖行为设为睡眠）。"
Write-Host " 2. 合盖就睡的笔记本，去「控制面板 → 电源选项 → 选择关闭盖子的功能」"
Write-Host "    把「关闭盖子时」设为【睡眠】而不是【休眠/关机】。"
Write-Host " 3. 常用排查命令："
Write-Host "      schtasks /query /tn `"$TaskName`" /fo LIST /v"
Write-Host "      powercfg /waketimers           查看已注册的唤醒定时器"
Write-Host "      powercfg /lastwake             查看上次是谁把电脑唤醒的"
Write-Host "      schtasks /run /tn `"$TaskName`"  立即手动跑一次试试"
Write-Host " 4. 卸载："
Write-Host "      powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
Write-Host ""
Write-Host " 建议现在手动跑一次验证（会真实登录预热，用 --dry-run 更安全）："
Write-Host "      cd `"$ProjectDir`"; & `"$PythonExe`" -m src.cli once --dry-run"
Write-Host "----------------------------------------------------------" -ForegroundColor Cyan
