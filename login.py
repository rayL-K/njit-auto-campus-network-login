# -*- coding: utf-8 -*-
"""
校园网自动登录脚本。

主流程：
1. 如果校园网门户不可达，先重连已保存的 Wi-Fi 配置。
2. 通过 /drcom/chkstatus 查询当前在线状态。
3. 若未恢复联网，则直接执行浏览器登录流程。
4. 校验是否为期望账号，并尽可能确认外网连通性。
5. 在脚本结束时弹出对应结果通知。

浏览器登录会优先尝试真实浏览器窗口交互，必要时再切换到无界面浏览器模式。
ChromeDriver 的本地维护默认开启，用于保证浏览器登录流程可用。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "data.json"
LOG_DIR = BASE_DIR / "logs"
ASSETS_DIR = BASE_DIR / "assets"
ICON_DIR = ASSETS_DIR / "icons"
APP_ICON_PATH = ASSETS_DIR / "campus_login.ico"
CHROMEDRIVER_PATH = BASE_DIR / "chromedriver.exe"

DEFAULT_PORTAL_ROOT = "http://172.31.255.156"
DEFAULT_WIFI_PROFILE = "B132YYDS"
CHROME_FOR_TESTING_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "known-good-versions-with-downloads.json"
)
DEFAULT_CONNECTIVITY_CHECKS = [
    {"url": "http://www.msftconnecttest.com/connecttest.txt", "keyword": "Microsoft Connect Test", "status": 200},
    {"url": "http://www.msftncsi.com/ncsi.txt", "keyword": "Microsoft NCSI", "status": 200},
    {"url": "http://connectivitycheck.gstatic.com/generate_204", "keyword": "", "status": 204},
    {"url": "http://cp.cloudflare.com/generate_204", "keyword": "", "status": 204},
]
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_RETRY_INTERVAL_SECONDS = 15
DEFAULT_MAX_RUNTIME_SECONDS = 15 * 60
DEFAULT_CONNECTIVITY_CONFIRM_TIMEOUT_SECONDS = 45
DEFAULT_CONNECTIVITY_CHECK_INTERVAL_SECONDS = 3
BROWSER_ELEMENT_SEARCH_POLL_INTERVAL_SECONDS = 0.5
BROWSER_ELEMENT_SEARCH_FRAME_DEPTH = 3
BROWSER_FORM_WAIT_TIMEOUT_SECONDS = 20
BROWSER_LOGOUT_WAIT_TIMEOUT_SECONDS = 8
BROWSER_PAGE_READY_TIMEOUT_SECONDS = 20
BROWSER_PAGE_STABILIZE_SECONDS = 1
BROWSER_POST_LOGOUT_WAIT_SECONDS = 5
BROWSER_POST_SUBMIT_WAIT_SECONDS = 5
BROWSER_POST_LOGIN_VERIFY_WAIT_SECONDS = 3
BROWSER_RELOCATE_LOGIN_BUTTON_WAIT_SECONDS = 5
BROWSER_WINDOW_RECT = (80, 60, 1280, 900)
DEFAULT_BROWSER_LOGIN_MODE_SEQUENCE = ("interactive", "headless")

TOAST_ICON_PATHS = {
    "Success": ICON_DIR / "success.svg",
    "Info": ICON_DIR / "info.svg",
    "Warning": ICON_DIR / "warning.svg",
    "Error": ICON_DIR / "error.svg",
}

POWERSHELL_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
Add-Type -AssemblyName System.Security

$title = [System.Security.SecurityElement]::Escape($env:CAMPUS_LOGIN_TITLE)
$message = [System.Security.SecurityElement]::Escape($env:CAMPUS_LOGIN_MESSAGE)
$appId = if ($env:CAMPUS_LOGIN_APP_ID) { $env:CAMPUS_LOGIN_APP_ID } else { 'PowerShell' }
$duration = if ($env:CAMPUS_LOGIN_DURATION -eq 'long') { 'long' } else { 'short' }
$imageUri = $env:CAMPUS_LOGIN_IMAGE_URI
$imageAlt = [System.Security.SecurityElement]::Escape($env:CAMPUS_LOGIN_IMAGE_ALT)
$imageXml = ""

if ($imageUri) {
    $imageXml = "<image placement='appLogoOverride' src='$imageUri' hint-crop='circle' alt='$imageAlt'/>"
}

$toastXml = @"
<toast duration="$duration">
  <visual>
    <binding template="ToastGeneric">
      $imageXml
      <text>$title</text>
      <text>$message</text>
    </binding>
  </visual>
</toast>
"@

$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml($toastXml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
$notifier.Show($toast)
Start-Sleep -Seconds 2
"""

POWERSHELL_BALLOON_NOTIFY_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$title = $env:CAMPUS_LOGIN_TITLE
$message = $env:CAMPUS_LOGIN_MESSAGE
$timeout = [Math]::Max([int]$env:CAMPUS_LOGIN_TIMEOUT, 3000)
$iconName = $env:CAMPUS_LOGIN_ICON
$iconPath = $env:CAMPUS_LOGIN_ICON_PATH

$systemIcon = switch ($iconName) {
    'Error'   { [System.Drawing.SystemIcons]::Error }
    'Warning' { [System.Drawing.SystemIcons]::Warning }
    'Success' { [System.Drawing.SystemIcons]::Information }
    default   { [System.Drawing.SystemIcons]::Information }
}

$balloonIcon = switch ($iconName) {
    'Error'   { [System.Windows.Forms.ToolTipIcon]::Error }
    'Warning' { [System.Windows.Forms.ToolTipIcon]::Warning }
    'Success' { [System.Windows.Forms.ToolTipIcon]::Info }
    default   { [System.Windows.Forms.ToolTipIcon]::Info }
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$customIcon = $null
if ($iconPath -and (Test-Path $iconPath)) {
    $customIcon = New-Object System.Drawing.Icon($iconPath)
    $notify.Icon = $customIcon
} else {
    $notify.Icon = $systemIcon
}
$notify.Visible = $true
$notify.BalloonTipTitle = $title
$notify.BalloonTipText = $message
$notify.BalloonTipIcon = $balloonIcon
$notify.ShowBalloonTip($timeout)
Start-Sleep -Milliseconds ($timeout + 1000)
$notify.Dispose()
if ($customIcon) { $customIcon.Dispose() }
"""

OPERATOR_SUFFIX_HINTS = {
    "校园用户": "",
    "校园网": "",
    "校园其他": "",
    "本科生": "",
}


@dataclass(frozen=True)
class BrowserElementHandle:
    frame_path: tuple[int, ...]
    element: Any


@dataclass(frozen=True)
class BrowserLoginMode:
    key: str
    display_name: str
    headless: bool
    allow_window_activation: bool = False
    allow_os_click: bool = False
    recover_from_logout_page: bool = False
    treat_missing_login_form_as_success_candidate: bool = False


BROWSER_LOGIN_MODES = {
    "interactive": BrowserLoginMode(
        key="interactive",
        display_name="真实浏览器登录",
        headless=False,
        allow_window_activation=True,
        allow_os_click=True,
        recover_from_logout_page=True,
    ),
    "headless": BrowserLoginMode(
        key="headless",
        display_name="无界面浏览器模式",
        headless=True,
        treat_missing_login_form_as_success_candidate=True,
    ),
}


class LoginError(RuntimeError):
    """登录失败的基础异常。"""


class RetryableLoginError(LoginError):
    """可重试的登录失败。"""


class NonRetryableLoginError(LoginError):
    """不可重试的登录失败，应立即停止。"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校园网门户自动登录。")
    parser.add_argument(
        "--status",
        action="store_true",
        help="仅查询并输出当前校园网状态。",
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        help="弹出一条通知测试消息后退出。",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="本次运行不弹出通知。",
    )
    parser.add_argument(
        "--skip-driver-update",
        action="store_true",
        help="成功后跳过 ChromeDriver 的本地维护。",
    )
    return parser.parse_args()


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"campus_login_{time.strftime('%Y%m%d')}.log"
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    logging.info("日志文件：%s", log_path)


def read_json_with_fallbacks(path: Path) -> dict[str, Any]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with path.open("r", encoding=encoding) as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = exc
    raise NonRetryableLoginError(f"读取 {path.name} 失败：{last_error}")


def as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    raw = read_json_with_fallbacks(path)

    user_id = str(raw.get("id", "")).strip()
    password = str(raw.get("password", ""))
    operator = str(raw.get("operator", "")).strip()
    account_suffix = str(raw.get("account_suffix", "")).strip()
    wifi_profile = str(raw.get("wifi_profile", DEFAULT_WIFI_PROFILE)).strip() or DEFAULT_WIFI_PROFILE
    portal_root = str(raw.get("portal_root", DEFAULT_PORTAL_ROOT)).strip().rstrip("/") or DEFAULT_PORTAL_ROOT
    expected_account = str(raw.get("expected_account", "")).strip()

    if not user_id or not password:
        raise NonRetryableLoginError("data.json 中必须包含非空的账号和密码。")

    checks = []
    raw_checks = raw.get("connectivity_checks", DEFAULT_CONNECTIVITY_CHECKS)
    if isinstance(raw_checks, list):
        for item in raw_checks:
            if isinstance(item, str) and item.strip():
                checks.append({"url": item.strip(), "keyword": ""})
            elif isinstance(item, dict) and item.get("url"):
                parsed_status = item.get("status")
                try:
                    parsed_status = int(parsed_status) if parsed_status is not None else None
                except (TypeError, ValueError):
                    parsed_status = None

                checks.append(
                    {
                        "url": str(item["url"]).strip(),
                        "keyword": str(item.get("keyword", "")).strip(),
                        "status": parsed_status,
                    }
                )
    if not checks:
        checks = DEFAULT_CONNECTIVITY_CHECKS

    max_runtime_seconds = raw.get("max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS)
    retry_interval_seconds = raw.get("retry_interval_seconds", DEFAULT_RETRY_INTERVAL_SECONDS)
    wifi_attempts = raw.get("wifi_attempts", 3)
    connectivity_confirm_timeout_seconds = raw.get(
        "connectivity_confirm_timeout_seconds",
        DEFAULT_CONNECTIVITY_CONFIRM_TIMEOUT_SECONDS,
    )
    connectivity_check_interval_seconds = raw.get(
        "connectivity_check_interval_seconds",
        DEFAULT_CONNECTIVITY_CHECK_INTERVAL_SECONDS,
    )
    try:
        max_runtime_seconds = max(60, int(max_runtime_seconds))
    except (TypeError, ValueError):
        max_runtime_seconds = DEFAULT_MAX_RUNTIME_SECONDS

    try:
        retry_interval_seconds = max(5, int(retry_interval_seconds))
    except (TypeError, ValueError):
        retry_interval_seconds = DEFAULT_RETRY_INTERVAL_SECONDS

    try:
        wifi_attempts = max(1, int(wifi_attempts))
    except (TypeError, ValueError):
        wifi_attempts = 3

    try:
        connectivity_confirm_timeout_seconds = max(0, int(connectivity_confirm_timeout_seconds))
    except (TypeError, ValueError):
        connectivity_confirm_timeout_seconds = DEFAULT_CONNECTIVITY_CONFIRM_TIMEOUT_SECONDS

    try:
        connectivity_check_interval_seconds = max(1, int(connectivity_check_interval_seconds))
    except (TypeError, ValueError):
        connectivity_check_interval_seconds = DEFAULT_CONNECTIVITY_CHECK_INTERVAL_SECONDS

    return {
        "user_id": user_id,
        "password": password,
        "operator": operator,
        "account_suffix": account_suffix,
        "expected_account": expected_account,
        "wifi_profile": wifi_profile,
        "portal_root": portal_root,
        "notify": as_bool(raw.get("notify"), True),
        "post_login_driver_update": as_bool(raw.get("post_login_driver_update"), True),
        "connectivity_checks": checks,
        "connectivity_confirm_timeout_seconds": connectivity_confirm_timeout_seconds,
        "connectivity_check_interval_seconds": connectivity_check_interval_seconds,
        "max_runtime_seconds": max_runtime_seconds,
        "retry_interval_seconds": retry_interval_seconds,
        "wifi_attempts": wifi_attempts,
    }


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "CampusAutoLogin/2.0"})
    # 忽略系统代理和环境变量（如 127.0.0.1:7897），直接建立连接
    session.trust_env = False
    return session


def to_file_uri(path: Path) -> str:
    """把本地路径转换为 Toast 可用的 file URI。"""
    return path.resolve().as_uri()


def get_toast_icon_uri(icon_name: str) -> str:
    """根据通知类型选择对应的 SVG 图标。"""
    icon_path = TOAST_ICON_PATHS.get(icon_name) or TOAST_ICON_PATHS.get("Info")
    if not icon_path or not icon_path.exists():
        return ""
    return to_file_uri(icon_path)


def mask_account(account: str) -> str:
    if not account:
        return "<未知账号>"
    if "@" in account:
        user, suffix = account.split("@", 1)
        return f"{user[:3]}***@{suffix}"
    return f"{account[:3]}***"


def send_notification(
    title: str,
    message: str,
    enabled: bool = True,
    icon: str = "Info",
    always_show_balloon: bool = False,
) -> None:
    if not enabled:
        logging.info("本次运行已禁用通知，跳过结果弹窗。")
        return
    if os.environ.get("USERNAME", "").upper() == "SYSTEM":
        logging.warning("当前任务以 SYSTEM 身份运行，无法显示桌面通知，已跳过。")
        return

    env = os.environ.copy()
    env["CAMPUS_LOGIN_TITLE"] = title[:64]
    env["CAMPUS_LOGIN_MESSAGE"] = message.replace("\r", " ").replace("\n", " ")[:240]
    env["CAMPUS_LOGIN_TIMEOUT"] = "5000"
    env["CAMPUS_LOGIN_ICON"] = icon
    env["CAMPUS_LOGIN_APP_ID"] = "PowerShell"
    env["CAMPUS_LOGIN_DURATION"] = "short"
    env["CAMPUS_LOGIN_IMAGE_URI"] = get_toast_icon_uri(icon)
    env["CAMPUS_LOGIN_IMAGE_ALT"] = title[:64]
    env["CAMPUS_LOGIN_ICON_PATH"] = str(APP_ICON_PATH) if APP_ICON_PATH.exists() else ""
    logging.info("准备发送通知：%s - %s", title, message)

    try:
        toast_result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_TOAST_SCRIPT],
            capture_output=True,
            text=True,
            timeout=12,
            env=env,
        )
        if toast_result.returncode != 0:
            logging.warning("Toast 通知发送失败，将继续尝试托盘气泡通知：%s", (toast_result.stderr or toast_result.stdout).strip())

        if always_show_balloon or toast_result.returncode != 0:
            balloon_result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_BALLOON_NOTIFY_SCRIPT],
                capture_output=True,
                text=True,
                timeout=12,
                env=env,
            )
            if balloon_result.returncode != 0:
                logging.warning("托盘气泡通知发送失败：%s", (balloon_result.stderr or balloon_result.stdout).strip())
        logging.info("通知发送流程结束。")
    except Exception as exc:
        logging.warning("Windows 通知流程异常：%s", exc)


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def portal_is_reachable(session: requests.Session, portal_root: str) -> bool:
    try:
        response = session.get(f"{portal_root}/", timeout=5)
        return response.status_code == 200
    except requests.RequestException:
        return False


def wait_for_portal(session: requests.Session, portal_root: str, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if portal_is_reachable(session, portal_root):
            return True
        time.sleep(2)
    return False


def disconnect_wifi() -> None:
    logging.info("正在主动断开当前 Wi-Fi 连接。")
    try:
        result = run_command(["netsh", "wlan", "disconnect"], timeout=15)
    except subprocess.TimeoutExpired:
        logging.warning("Wi-Fi 断开命令执行超时。")
        return

    output = " ".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if output:
        logging.info("netsh 输出：%s", output)


def connect_wifi(
    profile_name: str,
    session: requests.Session,
    portal_root: str,
    attempts: int,
    force_reconnect: bool = False,
) -> bool:
    if not force_reconnect and portal_is_reachable(session, portal_root):
        logging.info("校园网门户已可达，跳过 Wi-Fi 重连。")
        return True

    for attempt in range(1, attempts + 1):
        if force_reconnect:
            disconnect_wifi()
            time.sleep(2)

        logging.info("正在连接 Wi-Fi 配置 %s（第 %s/%s 次尝试）。", profile_name, attempt, attempts)
        try:
            result = run_command(["netsh", "wlan", "connect", f"name={profile_name}"], timeout=30)
        except subprocess.TimeoutExpired:
            logging.warning("Wi-Fi 连接命令执行超时。")
            result = None

        if result:
            output = " ".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
            if output:
                logging.info("netsh 输出：%s", output)

        if wait_for_portal(session, portal_root, timeout_seconds=20):
            logging.info("Wi-Fi 重连后，校园网门户已恢复可达。")
            return True

        time.sleep(3)

    return False


def refresh_wifi_connection(profile_name: str, session: requests.Session, portal_root: str, attempts: int) -> bool:
    logging.warning("校园网门户已认证，但外网仍未恢复，将尝试主动刷新 Wi-Fi 连接。")
    return connect_wifi(
        profile_name,
        session,
        portal_root,
        attempts,
        force_reconnect=True,
    )


def extract_js_string(html: str, name: str, default: str = "") -> str:
    patterns = [
        rf"{re.escape(name)}\s*=\s*'([^']*)'",
        rf'{re.escape(name)}\s*=\s*"([^"]*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1).strip()
    return default


def parse_carrier_suffixes(raw_carrier_json: str) -> dict[str, str]:
    if not raw_carrier_json:
        return {}

    try:
        data = json.loads(raw_carrier_json)
    except json.JSONDecodeError:
        return {}

    mapping: dict[str, str] = {}
    for group in data.values():
        if not isinstance(group, dict):
            continue
        for item in group.get("data", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            suffix = str(item.get("suffix", "")).strip()
            if name:
                mapping[name] = suffix
    return mapping


def parse_jsonp_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = re.match(r"^[^(]+\((.*)\)\s*;?$", stripped, re.DOTALL)
    if not match:
        raise RetryableLoginError(f"校园网门户返回了无法识别的 JSONP 响应：{stripped[:160]}")

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RetryableLoginError(f"解析校园网门户响应失败：{exc}") from exc


def fetch_portal_html(session: requests.Session, portal_root: str) -> str:
    try:
        response = session.get(f"{portal_root}/", timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise RetryableLoginError(f"校园网门户首页暂时无法访问：{exc}") from exc


def check_portal_status(session: requests.Session, portal_root: str) -> dict[str, Any]:
    try:
        response = session.get(
            f"{portal_root}/drcom/chkstatus",
            params={"callback": "campusStatus", "jsVersion": "4.X"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RetryableLoginError(f"查询校园网门户状态失败：{exc}") from exc

    return parse_jsonp_payload(response.text)


def portal_result_is_online(payload: dict[str, Any]) -> bool:
    return str(payload.get("result", "")).lower() in {"1", "ok", "true"}


def current_portal_account(payload: dict[str, Any]) -> str:
    return str(payload.get("uid") or payload.get("AC") or "").strip()


def infer_account_suffix(config: dict[str, Any], html: str, status: dict[str, Any]) -> str:
    explicit_suffix = config["account_suffix"]
    user_id = config["user_id"]
    if explicit_suffix:
        return explicit_suffix
    if "@" in user_id:
        return ""

    return ""


def build_login_account(config: dict[str, Any], suffix: str) -> str:
    user_id = config["user_id"]
    if "@" in user_id:
        return user_id
    return f"{user_id}{suffix}"


def account_matches_expected(config: dict[str, Any], current_account: str, expected_account: str) -> bool:
    if not current_account:
        return False

    configured_expected = config["expected_account"]
    if configured_expected and current_account.lower() == configured_expected.lower():
        return True
    if expected_account and current_account.lower() == expected_account.lower():
        return True
    return current_account.split("@", 1)[0] == config["user_id"]


def choose_v4ip(status: dict[str, Any], html: str) -> str:
    candidates = [
        status.get("v4ip"),
        status.get("v46ip"),
        status.get("ss5"),
        extract_js_string(html, "v4ip"),
        extract_js_string(html, "v46ip"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value != "000.000.000.000":
            return value
    return ""


def choose_v6ip(status: dict[str, Any], html: str) -> str:
    candidates = [status.get("v6ip"), extract_js_string(html, "v6ip")]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def describe_login_failure(payload: dict[str, Any]) -> str:
    for key in ("msg", "msga", "message", "error"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return json.dumps(
        {key: payload.get(key) for key in ("result", "ret_code", "uid") if key in payload},
        ensure_ascii=False,
    )


def is_invalid_credentials_error(payload: dict[str, Any]) -> bool:
    text = " ".join(str(payload.get(key, "")) for key in ("msg", "msga", "message")).lower()
    if str(payload.get("ret_code", "")).lower() == "1":
        return True
    keywords = ("密码", "账号", "用户名", "password", "account")
    return any(keyword in text for keyword in keywords)


def logout_via_http(session: requests.Session, config: dict[str, Any], html: str, status: dict[str, Any]) -> None:
    v4ip = choose_v4ip(status, html)
    v6ip = choose_v6ip(status, html)
    
    params = {
        "callback": "campusLogout",
        "wlan_user_ip": v4ip,
        "v4ip": v4ip,
        "v6ip": v6ip,
    }
    
    logging.info("尝试执行 HTTP 强制注销。")
    try:
        session.get(
            f"{config['portal_root']}/drcom/logout",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logging.warning("HTTP 注销请求异常：%s", exc)


def probe_external_connectivity_once(
    session: requests.Session,
    checks: list[dict[str, Any]],
) -> tuple[bool, str, str]:
    last_reason = "当前所有外网探测地址均未通过。"
    for check in checks:
        url = check["url"]
        keyword = check.get("keyword", "")
        expected_status = check.get("status")
        try:
            response = session.get(url, timeout=6, allow_redirects=False)
        except requests.RequestException as exc:
            reason = f"{url} 请求失败：{exc}"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue

        if expected_status is not None and response.status_code != expected_status:
            location = response.headers.get("location")
            reason = f"{url} 返回状态码 {response.status_code}，期望 {expected_status}"
            if location:
                reason = f"{reason}，跳转目标：{location}"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue

        if expected_status is None and not 200 <= response.status_code < 400:
            reason = f"{url} 返回状态码 {response.status_code}"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue
        if keyword and keyword not in response.text[:300]:
            preview = response.text[:80].replace("\r", " ").replace("\n", " ").strip()
            reason = f"{url} 返回内容未包含预期关键字"
            if preview:
                reason = f"{reason}，响应片段：{preview}"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue
        return True, url, ""

    return False, "", last_reason


def check_external_connectivity(
    session: requests.Session,
    checks: list[dict[str, Any]],
    confirm_timeout_seconds: int,
    check_interval_seconds: int,
) -> tuple[bool, str]:
    deadline = time.time() + max(0, confirm_timeout_seconds)
    attempt = 0
    last_reason = "当前所有外网探测地址均未通过。"

    while True:
        attempt += 1
        ok, checked_url, failure_reason = probe_external_connectivity_once(session, checks)
        if ok:
            logging.info("第 %s 次外网连通性检查成功：%s", attempt, checked_url)
            return True, checked_url

        last_reason = failure_reason or last_reason
        remaining_seconds = deadline - time.time()
        if remaining_seconds <= 0:
            break

        sleep_seconds = min(max(1, check_interval_seconds), max(1, int(remaining_seconds)))
        logging.info(
            "第 %s 次外网连通性检查未通过，将在 %s 秒后重试。",
            attempt,
            sleep_seconds,
        )
        time.sleep(sleep_seconds)

    logging.warning("在 %s 秒等待窗口内仍未确认外网连通性：%s", confirm_timeout_seconds, last_reason)
    return False, ""


def get_chrome_version() -> str | None:
    chrome_paths = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for chrome_path in chrome_paths:
        if not chrome_path.exists():
            continue
        try:
            escaped_path = str(chrome_path).replace("'", "''")
            ps_command = f"(Get-Item -LiteralPath '{escaped_path}').VersionInfo.ProductVersion"
            result = run_command(["powershell", "-NoProfile", "-Command", ps_command], timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            match = re.search(r"(\d+)\.", result.stdout.strip())
            if match:
                logging.info("检测到本机 Chrome 主版本号：%s", match.group(1))
                return match.group(1)

        try:
            result = run_command([str(chrome_path), "--version"], timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            match = re.search(r"(\d+)\.", result.stdout)
            if match:
                logging.info("检测到本机 Chrome 主版本号：%s", match.group(1))
                return match.group(1)
    return None


def get_local_chromedriver_version() -> str | None:
    if not CHROMEDRIVER_PATH.exists():
        return None
    try:
        result = run_command([str(CHROMEDRIVER_PATH), "--version"], timeout=10)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"ChromeDriver (\d+)", result.stdout)
    return match.group(1) if match else None


def download_chromedriver(chrome_major_version: str) -> bool:
    logging.info("正在获取适用于 Chrome %s 的 ChromeDriver 元数据。", chrome_major_version)
    download_session = requests.Session()
    download_session.trust_env = False
    try:
        response = download_session.get(CHROME_FOR_TESTING_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logging.warning("获取 ChromeDriver 元数据失败：%s", exc)
        return False

    matching_version = None
    for version in payload.get("versions", []):
        if str(version.get("version", "")).startswith(f"{chrome_major_version}."):
            matching_version = version
            break

    if not matching_version:
        logging.warning("未找到与 Chrome %s 对应的 ChromeDriver 版本。", chrome_major_version)
        return False

    download_url = ""
    for item in matching_version.get("downloads", {}).get("chromedriver", []):
        if item.get("platform") == "win64":
            download_url = item.get("url", "")
            break

    if not download_url:
        logging.warning("Chrome-for-Testing 未提供 win64 平台的 ChromeDriver 下载地址。")
        return False

    zip_path = BASE_DIR / "chromedriver.zip"
    try:
        with download_session.get(download_url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with zip_path.open("wb") as output_file:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        output_file.write(chunk)

        with zipfile.ZipFile(zip_path, "r") as archive:
            member = next(name for name in archive.namelist() if name.endswith("chromedriver.exe"))
            with archive.open(member) as source, CHROMEDRIVER_PATH.open("wb") as target:
                shutil.copyfileobj(source, target)

        logging.info("ChromeDriver 缓存已更新到版本 %s。", matching_version["version"])
        return True
    except Exception as exc:
        logging.warning("下载或更新 ChromeDriver 失败：%s", exc)
        return False
    finally:
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)


def maintain_local_chromedriver() -> None:
    chrome_version = get_chrome_version()
    if not chrome_version:
        logging.info("本机未检测到 Chrome，跳过 ChromeDriver 本地维护。")
        return

    driver_version = get_local_chromedriver_version()
    if driver_version == chrome_version:
        logging.info("当前 ChromeDriver 缓存已与本机 Chrome 版本匹配。")
        return

    logging.info("检测到 ChromeDriver 与本机 Chrome 版本不一致：Chrome=%s，ChromeDriver=%s", chrome_version, driver_version)
    download_chromedriver(chrome_version)


def init_browser(headless: bool = True):
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")

    errors = []
    if CHROMEDRIVER_PATH.exists():
        try:
            return webdriver.Chrome(service=Service(executable_path=str(CHROMEDRIVER_PATH)), options=options)
        except WebDriverException as exc:
            errors.append(f"本地 ChromeDriver 启动失败：{exc}")

    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as exc:
        errors.append(f"Selenium Manager 启动失败：{exc}")
        raise RetryableLoginError("；".join(errors)) from exc


def wait_for_browser_page_ready(driver, timeout_seconds: float = 15) -> None:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            ready_state = str(driver.execute_script("return document.readyState || 'loading';")).strip().lower()
            if ready_state in {"interactive", "complete"}:
                return
        except Exception:
            pass
        time.sleep(0.2)


def switch_to_browser_frame_path(driver, frame_path: tuple[int, ...]) -> None:
    from selenium.webdriver.common.by import By

    driver.switch_to.default_content()
    for index in frame_path:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        if index >= len(frames):
            raise RetryableLoginError(f"浏览器页面结构已变化，无法切换到 iframe 路径 {frame_path!r}。")
        driver.switch_to.frame(frames[index])


def collect_browser_frame_paths(
    driver,
    prefix: tuple[int, ...] = (),
    depth: int = 0,
    max_depth: int = BROWSER_ELEMENT_SEARCH_FRAME_DEPTH,
) -> list[tuple[int, ...]]:
    from selenium.webdriver.common.by import By

    paths = [prefix]
    if depth >= max_depth:
        return paths

    try:
        frame_count = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
    except Exception:
        return paths

    for index in range(frame_count):
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            if index >= len(frames):
                break
            driver.switch_to.frame(frames[index])
        except Exception:
            continue

        try:
            paths.extend(collect_browser_frame_paths(driver, prefix + (index,), depth + 1, max_depth))
        finally:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                switch_to_browser_frame_path(driver, prefix)

    return paths


def browser_element_is_usable(element, clickable: bool = False) -> bool:
    try:
        if not element.is_displayed():
            return False
        if clickable and not element.is_enabled():
            return False
        return True
    except Exception:
        return False


def find_browser_element_by_heuristic(driver, role: str, clickable: bool = False):
    return driver.execute_script(
        """
        const role = arguments[0];
        const requireEnabled = arguments[1];

        function attr(el, name) {
            return (el.getAttribute(name) || '').toLowerCase();
        }

        function blob(el) {
            return [
                el.id || '',
                el.name || '',
                el.className || '',
                attr(el, 'type'),
                attr(el, 'placeholder'),
                attr(el, 'aria-label'),
                attr(el, 'title'),
                attr(el, 'value'),
                (el.innerText || el.textContent || ''),
            ].join(' ').toLowerCase();
        }

        function visible(el) {
            if (!el || typeof el.getBoundingClientRect !== 'function') {
                return false;
            }
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden') {
                return false;
            }
            const rect = el.getBoundingClientRect();
            return rect.width >= 3 && rect.height >= 3;
        }

        function enabled(el) {
            return !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        }

        function formHasPassword(el) {
            const form = el.form || el.closest('form');
            return !!(form && form.querySelector('input[type="password"]'));
        }

        function containsAny(text, words) {
            return words.some((word) => text.includes(word));
        }

        function score(el) {
            const text = blob(el);
            const tag = (el.tagName || '').toLowerCase();
            const type = attr(el, 'type');
            let value = 0;

            if (role === 'account') {
                if (tag === 'input' || tag === 'textarea') {
                    value += 15;
                }
                if (type === '' || ['text', 'tel', 'number', 'email', 'search'].includes(type)) {
                    value += 20;
                }
                if (['hidden', 'password', 'submit', 'button', 'radio', 'checkbox'].includes(type)) {
                    value -= 80;
                }
                if (text.includes('ddddd')) {
                    value += 200;
                }
                if (formHasPassword(el)) {
                    value += 30;
                }
                if (containsAny(text, ['account', 'user', 'login', 'uid', 'userid', 'username', 'student', 'number', '学号', '账号', '帐号', '用户名'])) {
                    value += 60;
                }
            } else if (role === 'password') {
                if (tag === 'input') {
                    value += 15;
                }
                if (type === 'password') {
                    value += 200;
                }
                if (containsAny(text, ['password', 'pass', 'upass', 'pwd', '密码', '口令'])) {
                    value += 60;
                }
                if (formHasPassword(el)) {
                    value += 20;
                }
            } else if (role === 'login') {
                if (['button', 'input', 'a'].includes(tag) || attr(el, 'role') === 'button') {
                    value += 10;
                }
                if (type === 'submit') {
                    value += 120;
                }
                if (type === 'button') {
                    value += 40;
                }
                if (containsAny(text, ['0mkkey', 'login', 'signin', 'submit', 'connect', '认证', '登录', '上网', '联网', '连接'])) {
                    value += 100;
                }
                if (formHasPassword(el)) {
                    value += 25;
                }
            } else if (role === 'logout') {
                if (['button', 'input', 'a'].includes(tag) || attr(el, 'role') === 'button') {
                    value += 10;
                }
                if (type === 'submit' || type === 'button') {
                    value += 20;
                }
                if (containsAny(text, ['logout', 'signout', 'disconnect', '注销', '下线', '退出', '断开'])) {
                    value += 100;
                }
            }

            return value;
        }

        const selectors = {
            account: 'input, textarea',
            password: 'input',
            login: 'input, button, a, [role="button"]',
            logout: 'input, button, a, [role="button"]',
        };

        const candidates = Array.from(document.querySelectorAll(selectors[role] || '*'))
            .filter((el) => visible(el) && (!requireEnabled || enabled(el)))
            .map((el) => ({ el, score: score(el) }))
            .filter((item) => item.score > 0)
            .sort((left, right) => right.score - left.score);

        return candidates.length ? candidates[0].el : null;
        """,
        role,
        clickable,
    )


def find_first_browser_element_in_current_context(
    driver,
    locators: list[tuple[Any, str]],
    clickable: bool = False,
    heuristic: str | None = None,
):
    for locator in locators:
        try:
            elements = driver.find_elements(*locator)
        except Exception:
            continue

        for element in elements:
            if browser_element_is_usable(element, clickable):
                return element

    if heuristic:
        try:
            candidate = find_browser_element_by_heuristic(driver, heuristic, clickable)
        except Exception:
            candidate = None
        if candidate is not None and browser_element_is_usable(candidate, clickable):
            return candidate

    return None


def find_first_browser_element(
    driver,
    timeout_seconds: float,
    locators: list[tuple[Any, str]],
    clickable: bool = False,
    description: str = "元素",
    heuristic: str | None = None,
) -> BrowserElementHandle | None:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    checked_paths: list[tuple[int, ...]] = []

    while time.monotonic() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        checked_paths = collect_browser_frame_paths(driver)
        for frame_path in checked_paths:
            try:
                switch_to_browser_frame_path(driver, frame_path)
            except Exception:
                continue

            element = find_first_browser_element_in_current_context(
                driver,
                locators,
                clickable=clickable,
                heuristic=heuristic,
            )
            if element is not None:
                return BrowserElementHandle(frame_path=frame_path, element=element)

        time.sleep(BROWSER_ELEMENT_SEARCH_POLL_INTERVAL_SECONDS)

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    logging.debug("未能在浏览器页面中找到%s，已检查 iframe 路径：%s", description, checked_paths or [()])
    return None


def find_matching_browser_window(driver):
    try:
        import pygetwindow as gw
    except Exception as exc:
        logging.warning("未能加载窗口激活组件，将继续尝试默认前台窗口：%s", exc)
        return None

    rect = driver.get_window_rect()
    candidates = []
    for window in gw.getAllWindows():
        try:
            if abs(window.left - rect["x"]) > 150:
                continue
            if abs(window.top - rect["y"]) > 150:
                continue
            if abs(window.width - rect["width"]) > 250:
                continue
            if abs(window.height - rect["height"]) > 250:
                continue
            candidates.append(window)
        except Exception:
            continue

    if not candidates:
        title = ""
        try:
            title = driver.title or ""
        except Exception:
            title = ""
        if title:
            candidates = [window for window in gw.getWindowsWithTitle(title) if window.title]

    if not candidates:
        candidates = [window for window in gw.getWindowsWithTitle("Chrome") if window.title]

    if not candidates:
        return None

    return max(candidates, key=lambda item: max(1, item.width) * max(1, item.height))


def activate_browser_window(driver) -> bool:
    target = find_matching_browser_window(driver)
    if target is None:
        logging.warning("未找到可激活的 Chrome 窗口，将继续尝试默认前台输入。")
        return False

    try:
        if target.isMinimized:
            target.restore()
    except Exception:
        pass

    try:
        target.activate()
        time.sleep(0.5)
        return True
    except Exception as exc:
        logging.warning("激活 Chrome 窗口失败：%s", exc)
        return False


def switch_to_browser_element(driver, handle: BrowserElementHandle):
    switch_to_browser_frame_path(driver, handle.frame_path)
    return handle.element


def get_browser_element_screen_center(driver, handle: BrowserElementHandle) -> tuple[int, int]:
    element = switch_to_browser_element(driver, handle)
    metrics = driver.execute_script(
        """
        const target = arguments[0];
        target.scrollIntoView({block: 'center', inline: 'center'});
        const rect = target.getBoundingClientRect();
        let left = rect.left;
        let top = rect.top;
        let rootWindow = window;

        try {
            let currentWindow = window;
            while (currentWindow !== currentWindow.parent && currentWindow.frameElement) {
                const frameRect = currentWindow.frameElement.getBoundingClientRect();
                left += frameRect.left;
                top += frameRect.top;
                currentWindow = currentWindow.parent;
            }
            rootWindow = currentWindow;
        } catch (error) {
            rootWindow = window;
        }

        return {
            centerX: Math.max(((rootWindow.outerWidth - rootWindow.innerWidth) / 2), 0) + left + (rect.width / 2),
            centerY: Math.max((rootWindow.outerHeight - rootWindow.innerHeight - ((rootWindow.outerWidth - rootWindow.innerWidth) / 2)), 0) + top + (rect.height / 2),
            outerWidth: Math.max(rootWindow.outerWidth || 0, 1),
            outerHeight: Math.max(rootWindow.outerHeight || 0, 1),
        };
        """,
        element,
    )

    window = find_matching_browser_window(driver)
    if window is not None:
        window_left = window.left
        window_top = window.top
        window_width = max(1, window.width)
        window_height = max(1, window.height)
    else:
        rect = driver.get_window_rect()
        window_left = rect["x"]
        window_top = rect["y"]
        window_width = max(1, rect["width"])
        window_height = max(1, rect["height"])

    scale_x = window_width / max(1.0, float(metrics["outerWidth"]))
    scale_y = window_height / max(1.0, float(metrics["outerHeight"]))
    return (
        int(round(window_left + (float(metrics["centerX"]) * scale_x))),
        int(round(window_top + (float(metrics["centerY"]) * scale_y))),
    )


def browser_handles_reference_same_element(
    driver,
    first: BrowserElementHandle,
    second: BrowserElementHandle,
) -> bool:
    if first.frame_path != second.frame_path:
        return False

    try:
        switch_to_browser_frame_path(driver, first.frame_path)
        return bool(driver.execute_script("return arguments[0] === arguments[1];", first.element, second.element))
    except Exception:
        return first.element == second.element


def set_browser_input_value(driver, handle: BrowserElementHandle, text: str) -> None:
    element = switch_to_browser_element(driver, handle)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        element.click()
        element.clear()
        element.send_keys(text)
        return
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            const target = arguments[0];
            const value = arguments[1];
            target.scrollIntoView({block: 'center', inline: 'center'});
            target.focus();
            if ('value' in target) {
                target.value = value;
            }
            target.dispatchEvent(new Event('input', {bubbles: true}));
            target.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            element,
            text,
        )
    except Exception as exc:
        raise RetryableLoginError(f"浏览器已找到输入框，但填充内容失败：{exc}") from exc


def click_browser_element(driver, handle: BrowserElementHandle) -> None:
    element = switch_to_browser_element(driver, handle)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        element.click()
        return
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            arguments[0].scrollIntoView({block: 'center', inline: 'center'});
            arguments[0].click();
            """,
            element,
        )
    except Exception as exc:
        raise RetryableLoginError(f"浏览器已找到按钮，但触发点击失败：{exc}") from exc


def click_and_type_via_os_input(driver, handle: BrowserElementHandle, text: str) -> None:
    try:
        import pyautogui
    except Exception as exc:
        raise RetryableLoginError(f"加载 pyautogui 失败，无法执行模拟键盘输入：{exc}") from exc

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.15
    center_x, center_y = get_browser_element_screen_center(driver, handle)
    pyautogui.click(center_x, center_y)
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("backspace")
    pyautogui.write(text, interval=0.03)


def click_via_os_input(driver, handle: BrowserElementHandle) -> None:
    try:
        import pyautogui
    except Exception as exc:
        raise RetryableLoginError(f"加载 pyautogui 失败，无法执行模拟点击：{exc}") from exc

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.15
    center_x, center_y = get_browser_element_screen_center(driver, handle)
    pyautogui.click(center_x, center_y)


def submit_browser_login_form(
    driver,
    locators: dict[str, list[tuple[Any, str]]],
    mode_name: str,
    allow_os_click: bool = False,
) -> None:
    login_button = find_first_browser_element(
        driver,
        BROWSER_FORM_WAIT_TIMEOUT_SECONDS,
        locators["login"],
        clickable=True,
        description="登录按钮",
        heuristic="login",
    )

    if login_button is None:
        raise RetryableLoginError(f"{mode_name}未找到登录按钮。")

    logging.info("%s已重新定位登录按钮，优先尝试 DOM 点击提交。", mode_name)
    try:
        click_browser_element(driver, login_button)
        return
    except RetryableLoginError as exc:
        if not allow_os_click:
            raise RetryableLoginError(f"{mode_name}触发登录按钮失败：{exc}") from exc

        logging.warning("%sDOM 点击登录按钮失败，将回退到真实鼠标点击：%s", mode_name, exc)

    refreshed_button = find_first_browser_element(
        driver,
        BROWSER_RELOCATE_LOGIN_BUTTON_WAIT_SECONDS,
        locators["login"],
        clickable=True,
        description="登录按钮",
        heuristic="login",
    )
    if refreshed_button is not None:
        login_button = refreshed_button

    activate_browser_window(driver)
    click_via_os_input(driver, login_button)


def build_operator_match_terms(operator: str, suffix: str) -> list[str]:
    terms: list[str] = []

    def add(value: str) -> None:
        value = value.strip().lower()
        if value and value not in terms:
            terms.append(value)

    add(operator)
    add(suffix)

    alias_map = {
        "中国移动": ["移动", "cmcc", "校园移动"],
        "中国联通": ["联通", "lt", "校园联通"],
        "中国电信": ["电信", "dx", "校园电信"],
        "校园其他": ["其他", "校园用户", "校园网"],
    }

    normalized = operator.strip().lower()
    for key, aliases in alias_map.items():
        if key in operator or key.lower() in normalized:
            add(key)
            for alias in aliases:
                add(alias)

    return terms


def find_browser_operator_candidate(driver, operator: str, suffix: str, mode: str = "option"):
    return driver.execute_script(
        """
        const rawTerms = Array.isArray(arguments[0]) ? arguments[0] : [arguments[0]];
        const operatorHints = Array.from(new Set(rawTerms
            .map((item) => (item || '').trim().toLowerCase())
            .filter(Boolean)
            .concat([(arguments[1] || '').trim().toLowerCase()].filter(Boolean))));
        const suffix = (arguments[1] || '').trim().toLowerCase();
        const mode = (arguments[2] || 'option').trim().toLowerCase();

        function attr(el, name) {
            return (el.getAttribute(name) || '').trim().toLowerCase();
        }

        function textBlob(el) {
            return [
                el.innerText || '',
                el.textContent || '',
                el.value || '',
                attr(el, 'aria-label'),
                attr(el, 'title'),
                attr(el, 'placeholder'),
                attr(el, 'data-value'),
                attr(el, 'data-name'),
            ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }

        function visible(el) {
            if (!el || typeof el.getBoundingClientRect !== 'function') {
                return false;
            }
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden') {
                return false;
            }
            const rect = el.getBoundingClientRect();
            return rect.width >= 3 && rect.height >= 3;
        }

        function enabled(el) {
            return !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        }

        function clickable(el) {
            const tag = (el.tagName || '').toLowerCase();
            const role = attr(el, 'role');
            return ['button', 'a', 'label', 'summary', 'option', 'select'].includes(tag)
                || ['button', 'option', 'radio', 'combobox', 'tab'].includes(role)
                || attr(el, 'aria-haspopup') === 'listbox'
                || typeof el.onclick === 'function';
        }

        function containsAny(text, words) {
            return words.some((word) => word && text.includes(word));
        }

        const knownOperatorWords = ['中国移动', '中国联通', '中国电信', '移动', '联通', '电信', 'cmcc', 'lt', 'dx', '校园网', '校园用户'];
        const triggerWords = ['运营商', '接入方式', '网络类型', '上网方式', '宽带', '用户类型'];
        const selector = "input, button, a, label, div, span, li, td, select, option, [role='button'], [role='option'], [role='radio'], [role='combobox'], [aria-haspopup='listbox']";

        function score(el) {
            const tag = (el.tagName || '').toLowerCase();
            const role = attr(el, 'role');
            const text = textBlob(el);
            let value = 0;

            if (mode === 'option') {
                if (operatorHints.some((hint) => hint && text === hint)) {
                    value += 260;
                }
                if (operatorHints.some((hint) => hint && text.includes(hint))) {
                    value += 220;
                }
                if (suffix && [attr(el, 'value'), attr(el, 'data-value')].includes(suffix)) {
                    value += 260;
                }
                if (!clickable(el) && tag !== 'option' && value < 200) {
                    return -1;
                }
                if (['option', 'li', 'label', 'button', 'a'].includes(tag) || ['option', 'radio', 'button'].includes(role)) {
                    value += 40;
                }
                if (containsAny(text, triggerWords)) {
                    value -= 120;
                }
                return value;
            }

            if (!clickable(el)) {
                return -1;
            }
            if (containsAny(text, triggerWords)) {
                value += 180;
            }
            if (containsAny(text, knownOperatorWords)) {
                value += 40;
            }
            if (operatorHints.some((hint) => hint && text.includes(hint))) {
                value += 15;
            }
            if (role === 'combobox' || attr(el, 'aria-haspopup') === 'listbox' || tag === 'select') {
                value += 50;
            }
            return value;
        }

        const candidates = Array.from(document.querySelectorAll(selector))
            .filter((el) => visible(el) && enabled(el))
            .map((el) => ({ el, score: score(el) }))
            .filter((item) => item.score > 0)
            .sort((left, right) => right.score - left.score);

        return candidates.length ? candidates[0].el : null;
        """,
        build_operator_match_terms(operator, suffix),
        suffix,
        mode,
    )


def find_browser_operator_candidate_in_frames(
    driver,
    operator: str,
    suffix: str,
    mode: str,
    preferred_frame_path: tuple[int, ...] = (),
) -> BrowserElementHandle | None:
    frame_paths = [preferred_frame_path]
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    for frame_path in collect_browser_frame_paths(driver):
        if frame_path not in frame_paths:
            frame_paths.append(frame_path)

    for frame_path in frame_paths:
        try:
            switch_to_browser_frame_path(driver, frame_path)
        except Exception:
            continue

        candidate = find_browser_operator_candidate(driver, operator, suffix, mode=mode)
        if candidate is not None and browser_element_is_usable(candidate, clickable=True):
            return BrowserElementHandle(frame_path=frame_path, element=candidate)

    return None


def click_browser_candidate(driver, frame_path: tuple[int, ...], element: Any, allow_os_click: bool = False) -> None:
    handle = BrowserElementHandle(frame_path=frame_path, element=element)
    if allow_os_click:
        activate_browser_window(driver)
        click_via_os_input(driver, handle)
        return

    try:
        click_browser_element(driver, handle)
        return
    except RetryableLoginError:
        if not allow_os_click:
            raise

    activate_browser_window(driver)
    click_via_os_input(driver, handle)


def set_browser_operator(
    driver,
    operator: str,
    suffix: str,
    preferred_frame_path: tuple[int, ...] = (),
    allow_os_click: bool = False,
) -> bool:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    frame_paths = [preferred_frame_path]
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    for frame_path in collect_browser_frame_paths(driver):
        if frame_path not in frame_paths:
            frame_paths.append(frame_path)

    for frame_path in frame_paths:
        try:
            switch_to_browser_frame_path(driver, frame_path)
        except Exception:
            continue

        select_elements = driver.find_elements(By.CSS_SELECTOR, "select")
        for select_element in select_elements:
            if not browser_element_is_usable(select_element):
                continue

            try:
                selector = Select(select_element)
            except Exception:
                continue

            if suffix:
                for option in selector.options:
                    value = (option.get_attribute("value") or "").strip()
                    if value == suffix:
                        selector.select_by_value(value)
                        return True
            if operator:
                for option in selector.options:
                    option_text = option.text.strip()
                    if option_text == operator or operator in option_text:
                        selector.select_by_visible_text(option_text)
                        return True

        radio_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
        for radio in radio_inputs:
            if not browser_element_is_usable(radio, clickable=True):
                continue

            label_text = " ".join(
                filter(
                    None,
                    [
                        radio.get_attribute("value"),
                        radio.get_attribute("title"),
                        radio.get_attribute("aria-label"),
                    ],
                )
            )
            radio_id = (radio.get_attribute("id") or "").strip()
            if radio_id:
                try:
                    labels = driver.find_elements(By.CSS_SELECTOR, f"label[for='{radio_id}']")
                    label_text = " ".join(filter(None, [label_text, *[item.text.strip() for item in labels if item.text.strip()]]))
                except Exception:
                    pass

            if suffix and suffix == (radio.get_attribute("value") or "").strip():
                if not radio.is_selected():
                    click_browser_candidate(driver, frame_path, radio, allow_os_click=allow_os_click)
                return True
            if operator and operator in label_text:
                if not radio.is_selected():
                    click_browser_candidate(driver, frame_path, radio, allow_os_click=allow_os_click)
                return True

        custom_option_handle = find_browser_operator_candidate_in_frames(
            driver,
            operator,
            suffix,
            mode="option",
            preferred_frame_path=frame_path,
        )
        if custom_option_handle is not None:
            logging.info("浏览器已定位到运营商候选项，准备点击 %s。", operator)
            click_browser_candidate(
                driver,
                custom_option_handle.frame_path,
                custom_option_handle.element,
                allow_os_click=allow_os_click,
            )
            return True

        custom_trigger_handle = find_browser_operator_candidate_in_frames(
            driver,
            operator,
            suffix,
            mode="trigger",
            preferred_frame_path=frame_path,
        )
        if custom_trigger_handle is not None:
            logging.info("浏览器已定位到运营商选项栏，准备展开后选择 %s。", operator)
            click_browser_candidate(
                driver,
                custom_trigger_handle.frame_path,
                custom_trigger_handle.element,
                allow_os_click=allow_os_click,
            )

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                custom_option_handle = find_browser_operator_candidate_in_frames(
                    driver,
                    operator,
                    suffix,
                    mode="option",
                    preferred_frame_path=custom_trigger_handle.frame_path,
                )
                if custom_option_handle is not None:
                    logging.info("浏览器已在展开后的列表中定位到运营商 %s。", operator)
                    click_browser_candidate(
                        driver,
                        custom_option_handle.frame_path,
                        custom_option_handle.element,
                        allow_os_click=allow_os_click,
                    )
                    return True

            logging.warning("运营商选项栏已展开，但 3 秒内未找到选项 %s。", operator)
            return False

    return False


def get_browser_login_form_locators() -> dict[str, list[tuple[Any, str]]]:
    from selenium.webdriver.common.by import By

    lowered = "translate(%s,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"

    account_locators = [
        (By.NAME, "DDDDD"),
        (By.ID, "DDDDD"),
        (By.CSS_SELECTOR, "input[name='DDDDD'], input#DDDDD"),
        (By.XPATH, "//*[@id='edit_body']//input[@name='DDDDD']"),
        (By.XPATH, "//*[@id='edit_body']/div[3]/div[2]/form/input[2]"),
        (
            By.XPATH,
            "//input[not(@type='hidden') and ("
            + "contains("
            + lowered % "@name"
            + ",'ddddd') or contains("
            + lowered % "@name"
            + ",'user') or contains("
            + lowered % "@name"
            + ",'account') or contains("
            + lowered % "@name"
            + ",'login') or contains("
            + lowered % "@name"
            + ",'uid') or contains("
            + lowered % "@id"
            + ",'ddddd') or contains("
            + lowered % "@id"
            + ",'user') or contains("
            + lowered % "@id"
            + ",'account') or contains("
            + lowered % "@id"
            + ",'login') or contains("
            + lowered % "@id"
            + ",'uid') or contains(@placeholder,'学号') or contains(@placeholder,'账号') or contains(@placeholder,'用户名') or contains(@aria-label,'学号') or contains(@aria-label,'账号') or contains(@aria-label,'用户名'))]",
        ),
        (
            By.XPATH,
            "//form[.//input[@type='password']]//*[self::input or self::textarea]"
            "[not(@type='hidden') and not(@type='password') and not(@type='submit') and not(@type='button')][1]",
        ),
    ]

    password_locators = [
        (By.NAME, "upass"),
        (By.ID, "upass"),
        (By.CSS_SELECTOR, "input[name='upass'], input#upass, input[type='password']"),
        (By.XPATH, "//*[@id='edit_body']//input[@name='upass']"),
        (By.XPATH, "//*[@id='edit_body']/div[3]/div[2]/form/input[3]"),
        (By.XPATH, "//input[@type='password']"),
        (
            By.XPATH,
            "//input[contains("
            + lowered % "@name"
            + ",'pass') or contains("
            + lowered % "@name"
            + ",'pwd') or contains("
            + lowered % "@id"
            + ",'pass') or contains("
            + lowered % "@id"
            + ",'pwd') or contains(@placeholder,'密码') or contains(@aria-label,'密码')]",
        ),
    ]

    login_button_locators = [
        (By.NAME, "0MKKey"),
        (By.ID, "0MKKey"),
        (By.XPATH, "//*[@id='edit_body']/div[3]/div[2]/form/input[1]"),
        (By.CSS_SELECTOR, "input[name='0MKKey'], input#0MKKey"),
        (By.XPATH, "//input[@type='submit']"),
        (By.XPATH, "//button[@type='submit']"),
        (
            By.XPATH,
            "//input[contains("
            + lowered % "@value"
            + ",'login') or contains("
            + lowered % "@value"
            + ",'connect') or contains(@value,'登录') or contains(@value,'认证') or contains(@value,'上网') or contains(@value,'连接')]",
        ),
        (
            By.XPATH,
            "//button[contains("
            + lowered % "normalize-space(.)"
            + ",'login') or contains("
            + lowered % "normalize-space(.)"
            + ",'connect') or contains(normalize-space(.),'登录') or contains(normalize-space(.),'认证') or contains(normalize-space(.),'上网') or contains(normalize-space(.),'连接')]",
        ),
        (
            By.XPATH,
            "//form[.//input[@type='password']]//*[self::button or self::input][@type='submit' or @type='button'][1]",
        ),
    ]

    logout_button_locators = [
        (By.NAME, "logout"),
        (By.ID, "logout"),
        (By.CSS_SELECTOR, "input[name='logout'], input#logout"),
        (By.XPATH, "//input[@type='button' and contains(@value,'销')]"),
        (By.XPATH, "//input[contains(" + lowered % "@value" + ",'logout') or contains(@value,'注销') or contains(@value,'下线')]"),
        (
            By.XPATH,
            "//button[contains("
            + lowered % "normalize-space(.)"
            + ",'logout') or contains(normalize-space(.),'注销') or contains(normalize-space(.),'下线') or contains(normalize-space(.),'退出')]",
        ),
    ]

    return {
        "account": account_locators,
        "password": password_locators,
        "login": login_button_locators,
        "logout": logout_button_locators,
    }


def login_via_browser_mode(
    config: dict[str, Any],
    html: str,
    status: dict[str, Any],
    browser_mode: BrowserLoginMode,
) -> None:
    suffix = infer_account_suffix(config, html, status)
    account_input_value = build_login_account(config, suffix)
    locators = get_browser_login_form_locators()

    driver = init_browser(headless=browser_mode.headless)
    try:
        if browser_mode.allow_window_activation:
            driver.set_window_rect(*BROWSER_WINDOW_RECT)
        driver.get(f"{config['portal_root']}/")
        wait_for_browser_page_ready(driver, BROWSER_PAGE_READY_TIMEOUT_SECONDS)
        time.sleep(BROWSER_PAGE_STABILIZE_SECONDS)
        if browser_mode.allow_window_activation:
            activate_browser_window(driver)

        id_input = find_first_browser_element(
            driver,
            BROWSER_FORM_WAIT_TIMEOUT_SECONDS,
            locators["account"],
            description="账号输入框",
            heuristic="account",
        )

        if id_input is None and browser_mode.recover_from_logout_page:
            logout_button = find_first_browser_element(
                driver,
                BROWSER_LOGOUT_WAIT_TIMEOUT_SECONDS,
                locators["logout"],
                clickable=True,
                description="注销按钮",
                heuristic="logout",
            )
            if logout_button is not None:
                logging.info("%s当前处于注销页，先重新定位并点击注销按钮。", browser_mode.display_name)
                click_browser_candidate(
                    driver,
                    logout_button.frame_path,
                    logout_button.element,
                    allow_os_click=browser_mode.allow_os_click,
                )
                time.sleep(BROWSER_POST_LOGOUT_WAIT_SECONDS)
                wait_for_browser_page_ready(driver, BROWSER_PAGE_READY_TIMEOUT_SECONDS)
                if browser_mode.allow_window_activation:
                    activate_browser_window(driver)
                id_input = find_first_browser_element(
                    driver,
                    BROWSER_FORM_WAIT_TIMEOUT_SECONDS,
                    locators["account"],
                    description="账号输入框",
                    heuristic="account",
                )

        if id_input is None:
            if browser_mode.treat_missing_login_form_as_success_candidate:
                logging.info("%s未发现登录表单，校园网门户可能已经在线。", browser_mode.display_name)
                return

            page_title = ""
            try:
                page_title = driver.title
            except Exception:
                page_title = ""
            raise RetryableLoginError(f"{browser_mode.display_name}未发现登录表单。当前页面标题：{page_title or '<空>'}")

        if set_browser_operator(
            driver,
            config["operator"],
            suffix,
            preferred_frame_path=id_input.frame_path,
            allow_os_click=browser_mode.allow_os_click,
        ):
            logging.info("%s已自动选择运营商 %s。", browser_mode.display_name, config["operator"])

        password_input = find_first_browser_element(
            driver,
            BROWSER_FORM_WAIT_TIMEOUT_SECONDS,
            locators["password"],
            description="密码输入框",
            heuristic="password",
        )

        if password_input is None:
            raise RetryableLoginError(f"{browser_mode.display_name}未找到密码输入框。")

        if browser_handles_reference_same_element(driver, id_input, password_input):
            raise RetryableLoginError(
                f"{browser_mode.display_name}识别到账号输入框和密码输入框是同一个元素，已停止填写以避免误填。"
            )

        logging.info("开始执行%s：先填写账号密码，再重新定位并提交登录按钮。", browser_mode.display_name)
        set_browser_input_value(driver, id_input, account_input_value)
        set_browser_input_value(driver, password_input, config["password"])
        submit_browser_login_form(
            driver,
            locators,
            mode_name=browser_mode.display_name,
            allow_os_click=browser_mode.allow_os_click,
        )
        time.sleep(BROWSER_POST_SUBMIT_WAIT_SECONDS)
    finally:
        driver.quit()


def verify_portal_login_result(session: requests.Session, config: dict[str, Any], html: str) -> tuple[dict[str, Any], str, str]:
    verified_status = check_portal_status(session, config["portal_root"])
    expected_account = build_login_account(config, infer_account_suffix(config, html, verified_status))
    verified_account = current_portal_account(verified_status)
    return verified_status, expected_account, verified_account


def verify_browser_login_attempt(
    session: requests.Session,
    config: dict[str, Any],
    html: str,
    browser_mode: BrowserLoginMode,
) -> tuple[dict[str, Any], str, str]:
    verified_status, expected_account, verified_account = verify_portal_login_result(session, config, html)
    if not portal_result_is_online(verified_status):
        raise RetryableLoginError(f"{browser_mode.display_name}执行后，校园网门户仍显示离线。")
    if not account_matches_expected(config, verified_account, expected_account):
        raise RetryableLoginError(
            f"{browser_mode.display_name}未能切换到目标账号，当前账号为 {verified_account or '<未知>'}。"
        )
    return verified_status, expected_account, verified_account


def run_browser_login(session: requests.Session, config: dict[str, Any]) -> dict[str, Any]:
    logging.info("将执行浏览器登录流程。")
    if not portal_is_reachable(session, config["portal_root"]):
        connect_wifi(config["wifi_profile"], session, config["portal_root"], config["wifi_attempts"])

    html = fetch_portal_html(session, config["portal_root"])
    status = check_portal_status(session, config["portal_root"])
    if portal_result_is_online(status):
        logging.warning("进入浏览器登录前，校园网门户仍显示在线，将先执行注销并刷新 Wi-Fi。")
        logout_via_http(session, config, html, status)
        time.sleep(2)
        refresh_wifi_connection(
            config["wifi_profile"],
            session,
            config["portal_root"],
            config["wifi_attempts"],
        )
        html = fetch_portal_html(session, config["portal_root"])
        status = check_portal_status(session, config["portal_root"])

    browser_errors: list[str] = []
    selected_mode_key = ""
    verified_status: dict[str, Any] = status
    expected_account = ""
    verified_account = ""

    for mode_key in DEFAULT_BROWSER_LOGIN_MODE_SEQUENCE:
        browser_mode = BROWSER_LOGIN_MODES[mode_key]
        try:
            login_via_browser_mode(config, html, status, browser_mode)
            time.sleep(BROWSER_POST_LOGIN_VERIFY_WAIT_SECONDS)
            verified_status, expected_account, verified_account = verify_browser_login_attempt(
                session,
                config,
                html,
                browser_mode,
            )
            selected_mode_key = browser_mode.key
            break
        except RetryableLoginError as exc:
            browser_errors.append(f"{browser_mode.display_name}：{exc}")
            logging.warning("%s失败：%s", browser_mode.display_name, exc)

    if not selected_mode_key:
        raise RetryableLoginError("；".join(browser_errors) or "浏览器登录已执行完毕，但校园网门户仍显示离线。")

    connectivity_ok, checked_url = check_external_connectivity(
        session,
        config["connectivity_checks"],
        config["connectivity_confirm_timeout_seconds"],
        config["connectivity_check_interval_seconds"],
    )
    return {
        "account": verified_account or expected_account,
        "already_online": False,
        "used_browser_login": True,
        "browser_login_mode": selected_mode_key,
        "connectivity_ok": connectivity_ok,
        "connectivity_url": checked_url,
    }


def try_login_once(session: requests.Session, config: dict[str, Any]) -> dict[str, Any]:
    if not portal_is_reachable(session, config["portal_root"]):
        if not connect_wifi(config["wifi_profile"], session, config["portal_root"], config["wifi_attempts"]):
            raise RetryableLoginError(
                f"重连 Wi-Fi 配置 {config['wifi_profile']} 后，校园网门户仍然不可达。"
            )

    html = fetch_portal_html(session, config["portal_root"])
    status = check_portal_status(session, config["portal_root"])
    expected_account = build_login_account(config, infer_account_suffix(config, html, status))
    current_account = current_portal_account(status)

    if portal_result_is_online(status) and account_matches_expected(config, current_account, expected_account):
        connectivity_ok, checked_url = check_external_connectivity(
            session,
            config["connectivity_checks"],
            config["connectivity_confirm_timeout_seconds"],
            config["connectivity_check_interval_seconds"],
        )
        if connectivity_ok:
            return {
                "account": current_account or expected_account,
                "already_online": True,
                "used_browser_login": False,
                "connectivity_ok": True,
                "connectivity_url": checked_url,
            }

        logging.warning("校园网显示已在线，但外网检测不通（假死状态）。改用浏览器重新认证。")

    elif portal_result_is_online(status) and current_account:
        logging.warning(
            "校园网门户显示已有在线会话：%s。将改用浏览器重新认证。",
            current_account,
        )

    return run_browser_login(session, config)


def run_login_flow(session: requests.Session, config: dict[str, Any], notify_enabled: bool) -> dict[str, Any]:
    deadline = time.time() + config["max_runtime_seconds"]
    attempt = 0
    last_error: LoginError | None = None

    while time.time() < deadline:
        attempt += 1
        should_sleep_before_retry = True
        logging.info("开始执行第 %s 次登录尝试。", attempt)
        try:
            result = try_login_once(session, config)
            if result["connectivity_ok"]:
                return result

            connectivity_message = (
                f"校园网门户已认证为账号 {result['account']}，但外网仍未恢复，"
                "本次不会判定为成功。"
            )
            last_error = RetryableLoginError(connectivity_message)
            logging.warning("%s", connectivity_message)

            refreshed = refresh_wifi_connection(
                config["wifi_profile"],
                session,
                config["portal_root"],
                config["wifi_attempts"],
            )
            if refreshed:
                logging.info("Wi-Fi 刷新完成，将立即再次检查校园网状态。")
                should_sleep_before_retry = False
            else:
                last_error = RetryableLoginError(
                    f"{connectivity_message} 已尝试刷新 Wi-Fi，但校园网门户仍不可达。"
                )
        except NonRetryableLoginError:
            raise
        except RetryableLoginError as exc:
            last_error = exc
            logging.warning("第 %s 次登录尝试失败：%s", attempt, exc)

        if not should_sleep_before_retry:
            continue

        if time.time() + config["retry_interval_seconds"] >= deadline:
            break
        time.sleep(config["retry_interval_seconds"])

    raise last_error or RetryableLoginError("在设定的重试时间窗口内，校园网认证仍未成功。")


def show_status(session: requests.Session, config: dict[str, Any]) -> int:
    if not portal_is_reachable(session, config["portal_root"]):
        logging.info("校园网门户不可达，可能尚未连接校园 Wi-Fi，或当前不在校园网环境。")
        return 2

    html = fetch_portal_html(session, config["portal_root"])
    status = check_portal_status(session, config["portal_root"])
    expected_account = build_login_account(config, infer_account_suffix(config, html, status))
    current_account = current_portal_account(status) or "<离线>"
    online = portal_result_is_online(status)
    matches = account_matches_expected(config, current_account, expected_account)

    logging.info("校园网门户在线状态：%s", online)
    logging.info("当前账号：%s", current_account)
    logging.info("期望账号：%s", expected_account)
    logging.info("账号是否匹配：%s", matches)
    logging.info("当前 IPv4：%s", choose_v4ip(status, html) or "<未知>")
    return 0 if online else 1


def main() -> int:
    args = parse_args()
    setup_logging()
    config = load_config()
    notify_enabled = config["notify"] and not args.no_notify
    session = make_session()
    exit_code = 0
    result_title = ""
    result_message = ""
    result_icon = "Info"

    if args.notify_test:
        send_notification("校园网自动登录", "这是一条测试通知。", enabled=notify_enabled)
        logging.info("通知测试已完成。")
        return 0

    if args.status:
        return show_status(session, config)

    try:
        result = run_login_flow(session, config, notify_enabled)
    except NonRetryableLoginError as exc:
        logging.error("%s", exc)
        exit_code = 3
        result_title = "校园网自动登录失败"
        result_message = str(exc)
        result_icon = "Error"
    except RetryableLoginError as exc:
        logging.error("%s", exc)
        exit_code = 2
        result_title = "校园网自动登录失败"
        result_message = str(exc)
        result_icon = "Error"
    except Exception as exc:
        logging.exception("脚本发生未预期异常")
        exit_code = 4
        result_title = "校园网自动登录异常"
        result_message = str(exc)
        result_icon = "Error"
    else:
        account_text = result["account"]
        if result["already_online"]:
            result_message = f"校园网已在线，账号 {account_text} 无需重复认证。"
        elif result["used_browser_login"]:
            if result.get("browser_login_mode") == "interactive":
                result_message = f"校园网已恢复联网，账号 {account_text}，已通过真实浏览器完成登录。"
            else:
                result_message = f"校园网已恢复联网，账号 {account_text}，已通过无界面浏览器模式完成登录。"
        else:
            result_message = f"校园网已恢复联网，账号 {account_text}。"

        if result["connectivity_ok"]:
            result_message = f"{result_message} 外网连通性已确认。"
            result_icon = "Success"
        else:
            result_message = f"{result_message} 但外网连通性未完全确认。"
            result_icon = "Warning"

        result_title = "校园网自动登录成功"
        logging.info(result_message)

        if config["post_login_driver_update"] and not args.skip_driver_update and result["connectivity_ok"]:
            try:
                maintain_local_chromedriver()
            except Exception as exc:
                logging.warning("ChromeDriver 本地维护因异常被跳过：%s", exc)

    if result_title:
        send_notification(
            result_title,
            result_message,
            enabled=notify_enabled,
            icon=result_icon,
            always_show_balloon=True,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
