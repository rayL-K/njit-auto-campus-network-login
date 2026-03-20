# 南京工程学院自动连校园网脚本

## 项目简介

这是一个面向 Windows 的校园网自动登录脚本，适用于“夜间断网，次日自动恢复联网”的使用场景。

此脚本存在的意义，是尽量免去用户第二天还要手动打开认证页面、重复输入账号密码来连接校园网的繁琐过程。换句话说，它的目标不是替代校园网认证本身，而是把“重复、机械、低价值”的次日连网动作自动化，让电脑在合适的时间自行完成联网恢复。

典型使用方式如下：

- 夜里校园网进入限制时段后自动断网。
- 电脑保持开机、锁屏、黑屏、睡眠或休眠状态。
- Windows 计划任务在早上 `07:30` 触发脚本。
- 脚本自动重连 Wi-Fi、认证校园网，并在结束时弹出结果通知。

当前版本的主流程已经改为 **纯 HTTP 认证**，默认不会打开浏览器。

## 功能特性

- 自动检查校园网门户是否可达。
- 门户不可达时自动重连已保存的 Wi-Fi 配置。
- 通过 `drcom/chkstatus` 查询当前在线状态。
- 通过 `drcom/login` 直接完成认证。
- 认证完成后确认当前账号是否正确。
- 在等待窗口内重试确认外网连通性，避免刚恢复联网时误判。
- 脚本结束时统一弹出结果通知。
- 优先使用 Win10/Win11 原生 Toast 通知。
- Toast 不稳定时再补一个托盘气泡通知。
- 浏览器兜底能力保留，但默认关闭。
- ChromeDriver 本地维护能力保留，但默认关闭。

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

## 运行要求

### 系统要求

- Windows 10 或 Windows 11

### Python 依赖

当前环境至少需要：

- `requests`
- `selenium`

虽然主流程默认不走浏览器，但保留了可选兜底逻辑，因此仍建议保留 `selenium`。

### 本地前置条件

- 电脑已经保存校园 Wi-Fi 配置
- 账号密码可正常登录校园网
- Windows 用户会话存在，用于显示桌面通知

## 配置文件

请在本地创建 `data.json`，可参考 `data.example.json`。

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
  "account_suffix": "@cmcc",
  "wifi_profile": "B132YYDS",
  "notify": true,
  "enable_browser_fallback": false,
  "post_login_driver_update": false,
  "connectivity_confirm_timeout_seconds": 45,
  "connectivity_check_interval_seconds": 3,
  "max_runtime_seconds": 900,
  "retry_interval_seconds": 15
}
```

### 配置项说明

- `account_suffix`
  已知运营商后缀时建议显式填写，例如 `@cmcc`。
- `wifi_profile`
  已保存的 Wi-Fi 配置名称。
- `notify`
  是否启用桌面通知。
- `enable_browser_fallback`
  是否允许在 HTTP 登录失败后再尝试浏览器兜底。默认关闭。
- `post_login_driver_update`
  是否在联网成功后维护本地 ChromeDriver。默认关闭。
- `max_runtime_seconds`
  脚本最大运行时长。
- `retry_interval_seconds`
  登录失败后的重试间隔。
- `connectivity_confirm_timeout_seconds`
  认证完成后用于确认外网是否恢复的最长等待时间。
- `connectivity_check_interval_seconds`
  外网连通性检查的重试间隔。

## 执行流程

脚本运行时会按以下顺序执行：

1. 检查校园网门户是否可达。
2. 如果门户不可达，尝试重连已保存的 Wi-Fi。
3. 查询当前在线状态。
4. 如果已经是目标账号在线，则直接结束。
5. 如果未在线，则通过 HTTP 接口直接认证。
6. 再次校验当前在线账号是否正确。
7. 在等待窗口内重试确认外网连通性。
8. 在脚本结束时弹出结果通知。

## 通知机制

当前版本的通知策略如下：

- 优先发送 Windows 10/11 风格的 Toast 通知。
- 然后再补一个托盘气泡通知，提升可见性。
- 不再单独发送“启动通知”，只保留“结束结果通知”。
- `assets/icons/` 下为不同状态使用的 SVG 图标，`assets/campus_login.ico` 为统一项目图标。

结果通知覆盖以下情况：

- 已经在线，无需重复认证
- HTTP 认证成功
- 外网连通性未完全确认
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

当前脚本可以处理以下状态：

- 正常开机
- 锁屏
- 黑屏
- 睡眠
- 休眠

当前脚本不能处理以下状态：

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

## 故障排查

### 1. 脚本运行后打开了浏览器

默认情况下不会。

请检查：

- `data.json` 中是否手动设置了 `"enable_browser_fallback": true`
- 是否仍在运行旧版本脚本
- 是否手动调用了其他调试脚本

### 2. 脚本结束后没有看到通知

当前版本会在结束时发送两种通知：

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

### 4. HTTP 认证失败

请检查：

- `data.json` 中账号密码是否正确
- `account_suffix` 是否正确
- Wi-Fi 配置名称是否正确
- 校园网门户地址是否仍然可访问

## 版本控制说明

仓库中只保留源码与部署脚本。

以下内容不会提交到 Git：

- `data.json`
- `chromedriver.exe`
- `logs/`
- Python 缓存文件

## 许可说明

本项目使用 MIT 协议，详见 `LICENSE` 文件。
