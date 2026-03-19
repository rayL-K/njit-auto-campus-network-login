# Campus WiFi Auto Login

## Overview

`Campus WiFi Auto Login` is a Windows-based campus network automation script designed for scheduled morning reconnection.

The project targets the following scenario:

- The campus network is unavailable during the nightly restricted period.
- The computer remains powered on, locked, screen-off, sleeping, or hibernating overnight.
- Windows Task Scheduler wakes or resumes the machine around `07:30`.
- The script reconnects Wi-Fi, authenticates to the campus portal, and shows a Windows notification.

The current implementation no longer depends on `ChromeDriver` for the primary login path. Portal authentication is performed through direct HTTP requests, with Selenium kept only as a last-resort fallback.

## Features

- Direct HTTP login through the campus portal.
- Automatic Wi-Fi reconnection before authentication.
- Online-status verification through `drcom/chkstatus`.
- External connectivity confirmation after successful login.
- Windows 10/11 style Toast notification support.
- Balloon-tip notification fallback for compatibility.
- Optional ChromeDriver cache maintenance after network recovery.
- Scheduled Task friendly startup through `login.bat`.

## Repository Layout

- `login.py`
  Main application entrypoint.
- `login.bat`
  Windows launcher used by Task Scheduler.
- `register_task.ps1`
  Helper script for registering or updating the scheduled task.
- `data.example.json`
  Example configuration file without sensitive information.
- `data.json`
  Local runtime configuration file. This file is intentionally ignored by Git.

## Requirements

### Operating System

- Windows 10 or Windows 11

### Runtime

- Python environment with at least:
  - `requests`
  - `selenium`

### Local Requirements

- A saved campus Wi-Fi profile, for example `B132YYDS`
- A valid campus portal account
- A Windows user session available for desktop notifications

## Configuration

Create a local `data.json` file based on `data.example.json`.

### Minimal configuration

```json
{
  "id": "your-student-id",
  "password": "your-password",
  "operator": "中国移动"
}
```

### Recommended configuration

```json
{
  "id": "your-student-id",
  "password": "your-password",
  "operator": "中国移动",
  "account_suffix": "@cmcc",
  "wifi_profile": "B132YYDS",
  "notify": true,
  "enable_browser_fallback": true,
  "post_login_driver_update": true,
  "max_runtime_seconds": 900,
  "retry_interval_seconds": 15
}
```

### Important notes

- `account_suffix` is strongly recommended when the operator suffix is known.
- `data.json` contains secrets and is excluded from version control by `.gitignore`.
- `chromedriver.exe` is also excluded from Git because it is a local runtime binary, not project source.

## Execution Flow

The script runs in the following order:

1. Check whether the campus portal is reachable.
2. If the portal is unreachable, reconnect the saved Wi-Fi profile.
3. Query the current portal state through `drcom/chkstatus`.
4. If already online under the expected account, stop early.
5. Otherwise, authenticate through `drcom/login`.
6. Re-check portal state and verify the expected account.
7. Confirm external connectivity.
8. Send Windows notification.
9. Optionally refresh the local ChromeDriver cache after network recovery.

## Notifications

The project now prefers Windows 10/11 style Toast notifications.

### Behavior

- Primary mode: native Toast notification
- Fallback mode: legacy notification-area balloon tip

### Trigger points

- Task startup
- HTTP login failure before browser fallback
- Login success
- Retryable failure
- Non-retryable failure
- Unexpected exception

Use the following command to test the notification pipeline:

```cmd
login.bat --notify-test
```

## Scheduled Task Setup

Use the provided helper script to register or update the scheduled task:

```powershell
powershell -ExecutionPolicy Bypass -File .\register_task.ps1
```

The current recommended task configuration is:

- Daily at `07:30`
- `WakeToRun = true`
- `StartWhenAvailable = true`
- `RunOnlyIfNetworkAvailable = false`
- Run as the interactive logged-in user
- Highest available privileges

## Power and Sleep Requirements

The script can run successfully in these states:

- Screen off
- Locked session
- Normal running state
- Sleep
- Hibernate

The script cannot power on a fully shut down computer by itself.

If the machine is completely shut down, automatic morning recovery requires one of the following:

- BIOS/UEFI `RTC Wake` or `Resume by Alarm`
- Wake-on-LAN with another always-on device

### Recommended overnight setup

- Stay signed in
- Do not fully shut down the machine
- Allow the display to turn off if desired
- Use sleep or hibernate instead of shutdown when necessary
- Keep wake timers enabled in Windows power settings

## Common Commands

### Query current portal state

```cmd
login.bat --status
```

### Run the full login flow manually

```cmd
login.bat
```

### Test notifications only

```cmd
login.bat --notify-test
```

### Skip notifications for one run

```cmd
login.bat --no-notify
```

## Security

- Do not commit `data.json`.
- Do not store real credentials in `data.example.json`.
- Review scheduled task permissions periodically.
- Treat the GitHub repository as source code only, not a credential store.

## Troubleshooting

### The task does not wake the computer

Check the following:

- The machine is sleeping or hibernating, not shut down.
- Wake timers are enabled in the active Windows power plan.
- BIOS/UEFI wake support is enabled.
- The scheduled task still has `WakeToRun = true`.

### The script runs but does not show a notification

Check the following:

- The task is running in the interactive user session.
- The user is still signed in.
- Windows notifications are not disabled for the session.
- `login.bat --notify-test` succeeds.

### The script cannot authenticate

Check the following:

- `data.json` contains the correct account and password.
- `account_suffix` matches the portal's expected operator suffix.
- The Wi-Fi profile name is correct.
- The campus portal address is still reachable from the local network.

### ChromeDriver issues still appear

The primary path no longer depends on ChromeDriver.

If ChromeDriver problems appear, they are limited to the Selenium fallback path. In that case:

- Verify local Chrome is installed.
- Keep `chromedriver.exe` available locally.
- Allow post-login driver maintenance when the network is already online.

## Version Control

This repository is intended to track source code and deployment scripts only.

Local runtime artifacts are excluded, including:

- `data.json`
- `chromedriver.exe`
- `logs/`
- Python cache files

## License

No license file is currently defined for this project.
