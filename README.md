# 校园网自动登录说明

## 这次改了什么

现在主流程不再依赖 `Chrome` 和 `ChromeDriver` 才能联网。

新的执行顺序是：

1. 先检查校园网门户 `http://172.31.255.156`
2. 如果门户不可达，先尝试连接保存好的 Wi-Fi 配置
3. 通过 `drcom/chkstatus` 读取当前在线状态
4. 通过 `drcom/login` 直接发起 HTTP 登录
5. 登录成功后再做外网连通性确认
6. 最后才做 ChromeDriver 的缓存维护

这样就解决了原先“未联网时先去检测驱动，导致死锁”的问题。

## 已解决的问题

### 1. 离线时驱动检查卡死

已经解决。`login.py` 现在默认走 HTTP 直连认证，浏览器只作为最后兜底方案。

### 2. 睡眠/休眠状态下任务不稳定

已经针对任务计划程序改了推荐配置：

- `WakeToRun = true`
- `RunOnlyIfNetworkAvailable = false`
- `StartWhenAvailable = true`
- 运行用户改为当前登录用户
- 支持通知弹窗

### 3. 认证结果没有桌面提示

已经增加 Windows 通知区弹窗，以下情况会提示：

- 任务启动
- HTTP 登录失败，切到浏览器兜底
- 登录成功
- 认证失败
- 异常退出

## 仍然存在的物理限制

### 关机状态无法被 Python 脚本“自动开机”

这不是脚本问题，是硬件/BIOS/电源状态限制。

如果电脑是 **完全关机**，要想早上 7:30 自动联网，只能使用下面之一：

- BIOS/UEFI 里的 `RTC Wake` / `Resume by Alarm`
- Wake-on-LAN，并且局域网内有另一台始终在线的设备负责唤醒

如果你希望靠 Windows 计划任务完成这件事，电脑必须处于：

- 睡眠
- 休眠
- 或者已经开机且用户会话仍存在

最稳的做法是：

- 晚上不要关机，改为休眠
- BIOS 中启用唤醒定时器 / RTC 唤醒
- Windows 电源选项里允许定时器唤醒

## 文件说明

- `login.py`
  主程序。HTTP 登录、Wi-Fi 重连、状态检测、通知、浏览器兜底都在这里。
- `login.bat`
  计划任务调用入口。直接找 `D:\Anaconda\envs\web_login\python.exe`，不再依赖 `conda activate`。
- `register_task.ps1`
  用来注册/覆盖计划任务的脚本。
- `data.example.json`
  仓库里的示例配置。真实账号密码只放本机 `data.json`，不会提交到 GitHub。

## 推荐使用方式

### 1. 重新注册任务

在当前目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\register_task.ps1
```

默认会把任务时间设为 `07:30`。

### 2. 测试状态读取

```cmd
login.bat --status
```

### 3. 测试通知弹窗

```cmd
login.bat --notify-test
```

## 任务计划建议

建议任务运行方式是：

- `Run only when user is logged on`
- 当前用户运行
- 最高权限
- 允许唤醒计算机
- 不要勾选“仅在网络连接可用时运行”

原因很简单：

- 你还没认证之前，Windows 可能认为“网络不可用”
- 用 `SYSTEM` 账户运行时，桌面通知通常不会显示

## 可选配置

`data.json` 目前至少需要：

```json
{
  "id": "你的账号",
  "password": "你的密码",
  "operator": "中国移动"
}
```

也可以额外加这些可选项：

```json
{
  "account_suffix": "@cmcc",
  "wifi_profile": "B132YYDS",
  "max_runtime_seconds": 900,
  "retry_interval_seconds": 15,
  "notify": true,
  "enable_browser_fallback": true,
  "post_login_driver_update": true
}
```

如果你后续想彻底避免运营商识别歧义，建议直接在 `data.json` 里加：

```json
{
  "account_suffix": "@cmcc"
}
```
