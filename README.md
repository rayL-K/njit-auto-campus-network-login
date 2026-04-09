# 南京工程学院自动连校园网脚本

## 项目简介

这是一个面向 Windows 的校园网自动登录脚本，适用于“夜间断网，次日自动恢复联网”的使用场景。

此脚本存在的意义，是尽量免去用户第二天还要手动打开认证页面、重复输入账号密码来连接校园网的繁琐过程。换句话说，它的目标不是替代校园网认证本身，而是把“重复、机械、低价值”的次日连网动作自动化，让电脑在合适的时间自行完成联网恢复。

典型使用方式如下：

- 夜里校园网进入限制时段后自动断网。
- 电脑保持开机、锁屏、黑屏、睡眠或休眠状态。
- Windows 计划任务在早上 `07:30` 触发脚本。
- 脚本自动重连 Wi-Fi、认证校园网，并在结束时弹出结果通知。

当前实现中，真正的认证动作统一由浏览器完成。脚本不会直接通过 HTTP 接口提交账号密码，而是先检查校园网门户状态和 Wi-Fi 可达性，再打开门户页面完成账号填写、密码填写、运营商选择和登录按钮提交。

`requests` 仍然会参与登录前后的辅助流程，例如门户在线状态查询、必要时的会话注销、外网连通性确认和 Wi-Fi 恢复判断，但不负责实际提交登录表单。

## 快速开始

建议按下面顺序完成首次配置：

1. 在 Windows 上安装 Python `3.10+` 和 Chrome。
2. 安装运行依赖。
3. 复制 `data.example.json` 为 `data.json`，填入你的校园网账号信息。
4. 先手动执行一次状态查询和完整登录，确认无误后再注册计划任务。

```powershell
python -m pip install requests selenium pyautogui
Copy-Item .\data.example.json .\data.json
.\login.bat --status
.\login.bat
powershell -ExecutionPolicy Bypass -File .\register_task.ps1
```

`PowerShell` 中请使用 `.\login.bat` 调用批处理；如果你习惯使用 `cmd`，可直接写成 `login.bat`。

## 功能特性

- 启动后会先检查校园网门户是否可达，必要时自动重连已保存的 Wi-Fi 配置。
- 通过 `drcom/chkstatus` 查询当前在线状态、当前账号和 IP 信息，用于判断是否需要重新认证。
- 只要没有判定为“目标账号已在线且外网可用”，就会进入浏览器认证流程。
- 浏览器认证优先使用真实浏览器窗口：自动定位账号框、密码框、运营商控件和登录按钮，再通过页面 DOM 完成填写与提交。
- 如果真实浏览器路径失败，会自动切换到无界面浏览器模式重试同一套登录流程。
- 如果门户显示已有在线会话但账号不对，或虽然在线但外网探测不通，会先尝试注销当前会话并刷新 Wi-Fi，再重新打开浏览器认证。
- 认证完成后确认当前账号是否正确，并在等待窗口内重试确认外网连通性，避免刚恢复联网时误判。
- 默认使用多组 `HTTP/204` 探针确认外网，避免被 HTTPS 证书或本地代理干扰误判。
- 网络请求会忽略系统代理和环境变量代理，尽量避免被本机代理软件误导。
- 脚本结束时统一弹出结果通知，结果通知会同时尝试 Toast 与托盘气泡通知。
- ChromeDriver 本地维护默认开启；启动浏览器时也会先尝试本地 `chromedriver.exe`，失败后再交给 Selenium Manager。

## 目录说明

- `login.py`
  主程序，负责 Wi-Fi 重连、校园网认证、状态校验和通知弹窗。
- `login.bat`
  计划任务入口脚本，用于稳定调用 Python 环境。
- `register_task.ps1`
  用于注册或更新计划任务。
- `data.example.json`
  示例配置文件，不包含真实账号密码。
- `data.json`
  本机实际运行配置文件，不会提交到 Git。
- `assets/`
  通知图标和项目图标资源目录。
- `logs/`
  运行日志目录，日志文件按日期生成，例如 `campus_login_20260409.log`。

## 运行要求

### 系统要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 已安装可被 Selenium 正常启动的 Chrome

### Python 依赖

当前环境至少需要：

- `requests`
- `selenium`
- `pyautogui`（推荐，用于真实浏览器路径中的鼠标点击兜底）

主流程依赖浏览器自动化能力，因此建议保留 `selenium`，并确保本机安装了可被 Selenium 启动的 Chrome。`requests` 主要负责状态查询、会话注销和连通性探测，`pyautogui` 用于真实浏览器路径下的模拟点击兜底。

推荐安装命令：

```powershell
python -m pip install requests selenium pyautogui
```

### `login.bat` 的 Python 选择顺序

`login.bat` 会按下面顺序寻找 Python 解释器：

- `D:\Anaconda\envs\web_login\python.exe`
- `D:\Anaconda\python.exe`
- 当前 `PATH` 中的 `python`

如果你的 Python 安装位置不同，可以直接修改 `login.bat`，或者确保命令行里能直接执行 `python`。

### 本地前置条件

- 电脑已经保存校园 Wi-Fi 配置
- 账号密码可正常登录校园网
- Windows 用户会话存在，用于显示桌面通知

## 配置文件

请在本地创建 `data.json`，可参考 `data.example.json`。

```powershell
Copy-Item .\data.example.json .\data.json
```

### 最小配置

```json
{
  "id": "你的学号",
  "password": "你的密码",
  "operator": "中国移动"
}
```

### 推荐配置

```json
{
  "id": "你的学号",
  "password": "你的密码",
  "operator": "中国移动",
  "wifi_profile": "B132YYDS",
  "portal_root": "http://172.31.255.156",
  "notify": true,
  "post_login_driver_update": true,
  "wifi_attempts": 3,
  "connectivity_confirm_timeout_seconds": 45,
  "connectivity_check_interval_seconds": 3,
  "max_runtime_seconds": 900,
  "retry_interval_seconds": 15
}
```

### 核心配置项说明

`id` 和 `password` 为必填项；`operator` 在多数校园门户中建议填写，若登录页要求选择运营商，则应视为必填。

- `id`
  登录账号。可以直接填写学号，也可以填写带后缀的完整账号，例如 `xxxx@cmcc`。
- `password`
  校园网登录密码。
- `operator`
  推荐填写。登录页存在运营商下拉框、单选框或自定义选项时，脚本会用它匹配对应的运营商；如果你的门户不要求选择运营商，也可以留空。

### 配置项说明

- `account_suffix`
  仅当校园网门户明确要求账号后缀时再填写；如果你的登录页直接输入学号即可，建议留空。
- `expected_account`
  可选。用于更严格地校验登录后的在线账号；如果留空，脚本会优先按 `id + account_suffix` 判断，最后再退回到只比较 `@` 前的账号主体。
- `portal_root`
  校园网门户根地址，默认是 `http://172.31.255.156`。
- `wifi_profile`
  已保存的 Wi-Fi 配置名称。
- `notify`
  是否启用桌面通知。
- `post_login_driver_update`
  是否在联网成功后维护本地 ChromeDriver。默认开启。
- `wifi_attempts`
  门户不可达或刷新 Wi-Fi 时，每轮最多尝试连接 Wi-Fi 的次数。
- `max_runtime_seconds`
  脚本最大运行时长。
- `retry_interval_seconds`
  登录失败后的重试间隔。
- `connectivity_confirm_timeout_seconds`
  认证完成后用于确认外网是否恢复的最长等待时间。
- `connectivity_check_interval_seconds`
  外网连通性检查的重试间隔。
- `connectivity_checks`
  可自定义外网探测地址；单项支持 `url`、`keyword`、`status` 三个字段。

## 执行流程

脚本运行时会按以下顺序执行：

1. 检查校园网门户是否可达；如果不可达，先尝试重连已保存的 Wi-Fi。
2. 查询 `drcom/chkstatus`，读取当前在线状态、账号和 IP。
3. 如果已经是目标账号在线，再继续做外网探测；只有外网也恢复，才会直接判定成功并跳过浏览器认证。
4. 只要未满足“目标账号在线且外网可用”，就会转入浏览器认证流程。
5. 进入浏览器认证前，如果门户仍显示在线，会先执行一次 HTTP 注销并刷新 Wi-Fi，避免旧会话干扰新的浏览器登录。
6. 浏览器认证默认先尝试真实浏览器窗口：打开门户页面，定位账号框、密码框、运营商控件和登录按钮，然后填写并提交表单。
7. 如果真实浏览器路径失败，则自动切换到无界面浏览器模式，再执行同样的表单填写与提交流程。
8. 浏览器提交完成后，再次校验门户在线状态和当前账号是否已经切换到目标账号。
9. 在等待窗口内重试确认外网连通性；如果仍未恢复，会主动刷新 Wi-Fi 并继续重试，直到超时。
10. 在脚本结束时统一弹出结果通知，并在成功时按需维护本地 ChromeDriver。

## 通知机制

- 结束结果通知会先尝试发送 Windows 10/11 风格的 Toast 通知。
- 结束结果通知会额外补一个托盘气泡通知，提升可见性。
- `--notify-test` 主要用于验证通知链路，默认先尝试 Toast，失败时再回退气泡通知。
- `assets/icons/` 下为不同状态使用的 SVG 图标，`assets/campus_login.ico` 为统一项目图标。

结果通知覆盖以下情况：

- 已经在线，无需重复认证
- 浏览器认证成功
- 外网连通性未完全确认
- 门户已认证但外网仍未恢复，脚本继续重试
- 可重试失败
- 不可重试失败
- 脚本异常退出

测试通知命令：

```cmd
login.bat --notify-test
```

## 计划任务配置

建议使用项目自带脚本注册计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\register_task.ps1
```

推荐任务配置如下：

- 每天 `07:30`
- `WakeToRun = true`
- `StartWhenAvailable = true`
- `RunOnlyIfNetworkAvailable = false`
- 使用当前登录用户运行
- 以最高权限运行

## 电源与休眠说明

支持的状态：

- 正常开机
- 黑屏
- 睡眠
- 休眠

存在额外限制的状态：

- 锁屏
  无界面浏览器路径仍可能运行，但“真实浏览器模拟键鼠”策略无法工作。
- 注销
  不存在可交互桌面，会导致真实浏览器模拟键鼠策略失效。

不支持的状态：

- 完全关机

如果电脑是完全关机状态，脚本本身无法开机，必须依赖：

- BIOS/UEFI 的定时开机功能，例如 `RTC Wake`、`Resume by Alarm`
- 或者 Wake-on-LAN

### 推荐夜间状态

最稳的方式是：

- 保持用户已登录
- 不要完全关机
- 允许显示器熄灭
- 需要省电时用睡眠或休眠，而不是关机

如果你启用了“真实浏览器模拟键鼠优先”这条策略，还需要额外注意：

- 黑屏可以，锁屏和注销不行
- 必须保留可交互的桌面会话
- 一旦进入锁屏界面，脚本只能退回无界面浏览器路径，不能真的帮你点界面

### 建议同步调整的 Windows 设置

为了让“黑屏但不锁屏”的运行方式更稳定，建议同时检查并调整这些系统设置：

- 关闭屏幕保护程序
- 关闭“恢复时显示登录屏幕”或同类选项
- 关闭“唤醒时需要密码”
- 保持用户已登录，不要手动按 `Win + L`
- 允许显示器自动关闭，但不要依赖锁屏来省电
- 如果使用睡眠或休眠，确保唤醒后直接回到桌面会话

一句话概括就是：

- 推荐“已登录 + 黑屏/熄屏 + 可唤醒”
- 不推荐“锁屏 + 等脚本自己点界面”

## 常用命令

### 查询当前校园网状态

```cmd
login.bat --status
```

### 手动执行完整登录流程

```cmd
login.bat
```

### 测试通知

```cmd
login.bat --notify-test
```

### 本次运行不弹通知

```cmd
login.bat --no-notify
```

### 本次跳过 ChromeDriver 本地维护

```cmd
login.bat --skip-driver-update
```

## 日志与退出码

- 日志默认写入 `logs/` 目录，文件名格式为 `campus_login_YYYYMMDD.log`。
- 控制台输出和日志文件会同时保留，便于排查计划任务场景下的问题。
- 常见退出码如下：
- `0`：执行成功；或 `--status` 查询到校园网已在线。
- `1`：`--status` 查询到校园网门户可达，但当前未在线。
- `2`：`--status` 时校园网门户不可达；或完整登录流程在重试窗口内仍未成功。
- `3`：不可重试失败，常见于配置错误、账号密码问题或页面返回明确错误。
- `4`：脚本出现未预期异常。

## 故障排查

### 1. 脚本运行后打开了浏览器

只要脚本没有判定为“目标账号已在线且外网可用”，就会进入浏览器认证，这是当前主流程。

如果你看到的不是登录页，而是异常空白页或错误页，请检查：

- 是否仍在运行旧版本脚本
- Chrome 与 ChromeDriver 是否可正常启动
- 校园网门户地址是否仍可访问

### 2. 脚本结束后没有看到通知

脚本结束时会发送两种通知：

- Toast 通知
- 托盘气泡通知

如果仍然看不到，请检查：

- 当前任务是否在交互用户会话中运行
- 当前用户是否仍处于登录状态
- 系统是否关闭了通知

### 3. 计划任务没有唤醒电脑

请检查：

- 电脑是否只是睡眠或休眠，而不是关机
- 当前电源计划是否允许唤醒定时器
- BIOS/UEFI 是否启用了相关唤醒能力
- 计划任务是否仍保持 `WakeToRun = true`

### 4. 浏览器认证失败或账号不对

请检查：

- `data.json` 中账号密码是否正确
- `operator`、`account_suffix`、`expected_account` 是否符合当前门户页面
- Wi-Fi 配置名称是否正确
- 校园网门户地址是否仍然可访问
- Chrome 是否能正常启动，`selenium` 是否已安装
- 如果当前处于锁屏或注销状态，真实浏览器路径可能失败，只能依赖无界面浏览器兜底
- 查看 `logs/` 目录下当天日志，确认失败发生在“重新定位登录按钮”“账号校验”还是“外网探测”阶段

## 许可说明

本项目使用 MIT 协议，详见 `LICENSE` 文件。
