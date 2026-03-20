# -*- coding: utf-8 -*-
"""
校园网自动登录脚本。

主流程：
1. 如果校园网门户不可达，先重连已保存的 Wi-Fi 配置。
2. 通过 /drcom/chkstatus 查询当前在线状态。
3. 通过 /drcom/login 直接完成 HTTP 认证，不打开浏览器。
4. 校验是否为期望账号，并尽可能确认外网连通性。
5. 在脚本结束时弹出对应结果通知。

Selenium 仅作为可选兜底能力保留，默认关闭。
ChromeDriver 的本地维护也默认关闭，避免任何不必要的浏览器相关动作。
"""

from __future__ import annotations

import argparse
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
    {"url": "http://www.msftconnecttest.com/connecttest.txt", "keyword": "Microsoft Connect Test"},
    {"url": "https://www.baidu.com", "keyword": ""},
]
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_RETRY_INTERVAL_SECONDS = 15
DEFAULT_MAX_RUNTIME_SECONDS = 15 * 60
DEFAULT_CONNECTIVITY_CONFIRM_TIMEOUT_SECONDS = 45
DEFAULT_CONNECTIVITY_CHECK_INTERVAL_SECONDS = 3

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
    "中国移动": "@cmcc",
    "移动": "@cmcc",
    "cmcc": "@cmcc",
    "中国电信": "@dx",
    "电信": "@dx",
    "dx": "@dx",
    "中国联通": "@lt",
    "联通": "@lt",
    "lt": "@lt",
    "校园用户": "",
    "校园网": "",
    "校园其他": "",
    "本科生": "",
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
                checks.append(
                    {
                        "url": str(item["url"]).strip(),
                        "keyword": str(item.get("keyword", "")).strip(),
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
        "post_login_driver_update": as_bool(raw.get("post_login_driver_update"), False),
        "enable_browser_fallback": as_bool(raw.get("enable_browser_fallback"), False),
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

    current_account = current_portal_account(status)
    if current_account.startswith(f"{user_id}@"):
        return current_account[len(user_id):]

    carrier_map = parse_carrier_suffixes(extract_js_string(html, "carrier"))
    operator = config["operator"]
    if operator in carrier_map:
        return carrier_map[operator]

    normalized = operator.lower()
    for key, suffix in OPERATOR_SUFFIX_HINTS.items():
        if key in operator or key in normalized:
            return suffix

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


def login_via_http(
    session: requests.Session,
    config: dict[str, Any],
    html: str,
    status: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    suffix = infer_account_suffix(config, html, status)
    account = build_login_account(config, suffix)
    v4ip = choose_v4ip(status, html)
    v6ip = choose_v6ip(status, html) or "::"
    js_version = extract_js_string(html, "fileVersion", "4.X") or "4.X"

    if not v4ip and not v6ip:
        raise RetryableLoginError("校园网门户页面未提供当前设备 IP，暂时无法发起认证。")

    params = {
        "callback": "campusLogin",
        "DDDDD": account,
        "upass": config["password"],
        "0MKKey": "123456",
        "R1": "",
        "R2": "",
        "R3": "",
        "R6": "0",
        "para": "00",
        "v4ip": v4ip,
        "v6ip": v6ip,
        "terminal_type": "1",
        "lang": "zh-cn",
        "jsVersion": js_version,
    }

    logging.info("准备直接向校园网门户发起 HTTP 认证，账号 %s。", mask_account(account))

    try:
        response = session.get(
            f"{config['portal_root']}/drcom/login",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RetryableLoginError(f"向校园网门户发起 HTTP 认证请求失败：{exc}") from exc

    payload = parse_jsonp_payload(response.text)
    if portal_result_is_online(payload):
        return account, payload

    description = describe_login_failure(payload)
    if is_invalid_credentials_error(payload):
        raise NonRetryableLoginError(f"校园网门户拒绝了当前账号或密码：{description}")
    raise RetryableLoginError(f"校园网门户登录未成功：{description}")


def probe_external_connectivity_once(
    session: requests.Session,
    checks: list[dict[str, str]],
) -> tuple[bool, str, str]:
    last_reason = "当前所有外网探测地址均未通过。"
    for check in checks:
        url = check["url"]
        keyword = check.get("keyword", "")
        try:
            response = session.get(url, timeout=6, allow_redirects=True)
        except requests.RequestException as exc:
            reason = f"{url} 请求失败：{exc}"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue

        if not 200 <= response.status_code < 400:
            reason = f"{url} 返回状态码 {response.status_code}"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue
        if keyword and keyword not in response.text[:300]:
            reason = f"{url} 返回内容未包含预期关键字"
            logging.info("外网探测未通过：%s", reason)
            last_reason = reason
            continue
        return True, url, ""

    return False, "", last_reason


def check_external_connectivity(
    session: requests.Session,
    checks: list[dict[str, str]],
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
    try:
        response = requests.get(CHROME_FOR_TESTING_URL, timeout=30)
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
        with requests.get(download_url, stream=True, timeout=60) as response:
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


def init_browser():
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--window-size=1280,900")

    errors = []
    if CHROMEDRIVER_PATH.exists():
        try:
            return webdriver.Chrome(service=Service(executable_path=str(CHROMEDRIVER_PATH)), options=options)
        except WebDriverException as exc:
            errors.append(f"local ChromeDriver failed: {exc}")

    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as exc:
        errors.append(f"Selenium Manager failed: {exc}")
        raise RetryableLoginError(" ; ".join(errors)) from exc


def set_browser_operator(driver, operator: str, suffix: str) -> bool:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    select_elements = driver.find_elements(By.NAME, "ISP_select")
    if not select_elements:
        select_elements = driver.find_elements(By.XPATH, "//*[@id='edit_body']//select")

    if select_elements:
        selector = Select(select_elements[0])
        if suffix:
            for option in selector.options:
                if option.get_attribute("value") == suffix:
                    selector.select_by_value(suffix)
                    return True
        if operator:
            for option in selector.options:
                if option.text.strip() == operator:
                    selector.select_by_visible_text(operator)
                    return True

    radio_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
    for radio in radio_inputs:
        if suffix and radio.get_attribute("value") == suffix:
            if not radio.is_selected():
                radio.click()
            return True
        if operator and operator in (radio.get_attribute("title") or ""):
            if not radio.is_selected():
                radio.click()
            return True

    return False


def login_via_browser_fallback(config: dict[str, Any], html: str, status: dict[str, Any]) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    suffix = infer_account_suffix(config, html, status)
    account_input_value = config["user_id"]
    if "@" not in account_input_value and not suffix:
        account_input_value = build_login_account(config, suffix)

    driver = init_browser()
    try:
        driver.get(f"{config['portal_root']}/")
        wait = WebDriverWait(driver, 20)
        time.sleep(3)

        id_input = None
        password_input = None
        login_button = None

        for locator in [
            (By.NAME, "DDDDD"),
            (By.XPATH, "//*[@id='edit_body']//input[@name='DDDDD']"),
            (By.XPATH, "//*[@id='edit_body']/div[3]/div[2]/form/input[2]"),
        ]:
            try:
                id_input = wait.until(EC.visibility_of_element_located(locator))
                break
            except Exception:
                continue

        if id_input is None:
            logging.info("浏览器兜底未发现登录表单，校园网门户可能已经在线。")
            return

        for locator in [
            (By.NAME, "upass"),
            (By.XPATH, "//*[@id='edit_body']//input[@name='upass']"),
            (By.XPATH, "//*[@id='edit_body']/div[3]/div[2]/form/input[3]"),
        ]:
            try:
                password_input = wait.until(EC.visibility_of_element_located(locator))
                break
            except Exception:
                continue

        if password_input is None:
            raise RetryableLoginError("浏览器兜底未找到密码输入框。")

        operator_applied = set_browser_operator(driver, config["operator"], suffix)
        if not operator_applied and suffix:
            account_input_value = build_login_account(config, suffix)

        for locator in [
            (By.NAME, "0MKKey"),
            (By.XPATH, "//*[@id='edit_body']/div[3]/div[2]/form/input[1]"),
        ]:
            try:
                login_button = wait.until(EC.element_to_be_clickable(locator))
                break
            except Exception:
                continue

        if login_button is None:
            raise RetryableLoginError("浏览器兜底未找到登录按钮。")

        id_input.clear()
        id_input.send_keys(account_input_value)
        password_input.clear()
        password_input.send_keys(config["password"])
        login_button.click()
        time.sleep(5)
    finally:
        driver.quit()


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
        return {
            "account": current_account or expected_account,
            "already_online": True,
            "used_browser_fallback": False,
            "connectivity_ok": connectivity_ok,
            "connectivity_url": checked_url,
        }

    if portal_result_is_online(status) and current_account:
        logging.warning(
            "校园网门户显示已有其他会话在线：%s。将尝试切换为目标账号 %s。",
            current_account,
            expected_account,
        )

    account, _ = login_via_http(session, config, html, status)
    time.sleep(3)

    verified_status = check_portal_status(session, config["portal_root"])
    verified_account = current_portal_account(verified_status)
    if not portal_result_is_online(verified_status):
        raise RetryableLoginError("HTTP 认证请求已发送，但校园网门户仍显示离线。")
    if not account_matches_expected(config, verified_account, account):
        raise RetryableLoginError(
            f"校园网门户已显示在线，但当前账号为 {verified_account or '<未知>'}，与期望账号不一致。"
        )

    connectivity_ok, checked_url = check_external_connectivity(
        session,
        config["connectivity_checks"],
        config["connectivity_confirm_timeout_seconds"],
        config["connectivity_check_interval_seconds"],
    )
    return {
        "account": verified_account or account,
        "already_online": False,
        "used_browser_fallback": False,
        "connectivity_ok": connectivity_ok,
        "connectivity_url": checked_url,
    }


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

    if config["enable_browser_fallback"]:
        logging.warning("直接 HTTP 认证未成功，将尝试浏览器兜底方案。")
        if not portal_is_reachable(session, config["portal_root"]):
            connect_wifi(config["wifi_profile"], session, config["portal_root"], config["wifi_attempts"])

        html = fetch_portal_html(session, config["portal_root"])
        status = check_portal_status(session, config["portal_root"])
        login_via_browser_fallback(config, html, status)
        time.sleep(3)

        verified_status = check_portal_status(session, config["portal_root"])
        expected_account = build_login_account(config, infer_account_suffix(config, html, verified_status))
        verified_account = current_portal_account(verified_status)
        if not portal_result_is_online(verified_status):
            raise last_error or RetryableLoginError("浏览器兜底已执行完毕，但校园网门户仍显示离线。")
        if not account_matches_expected(config, verified_account, expected_account):
            raise last_error or RetryableLoginError(
                f"浏览器兜底未能切换到目标账号，当前账号为 {verified_account or '<未知>'}。"
            )

        connectivity_ok, checked_url = check_external_connectivity(
            session,
            config["connectivity_checks"],
            config["connectivity_confirm_timeout_seconds"],
            config["connectivity_check_interval_seconds"],
        )
        return {
            "account": verified_account or expected_account,
            "already_online": False,
            "used_browser_fallback": True,
            "connectivity_ok": connectivity_ok,
            "connectivity_url": checked_url,
        }

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
        elif result["used_browser_fallback"]:
            result_message = f"校园网已恢复联网，账号 {account_text}，使用了浏览器兜底。"
        else:
            result_message = f"校园网已恢复联网，账号 {account_text}，未打开浏览器，直接通过 HTTP 认证成功。"

        if result["connectivity_ok"]:
            result_message = f"{result_message} 外网连通性已确认。"
            result_icon = "Success"
        else:
            result_message = f"{result_message} 但外网连通性未完全确认。"
            result_icon = "Warning"

        result_title = "校园网自动登录成功"
        logging.info(result_message)

        if (
            config["enable_browser_fallback"]
            and config["post_login_driver_update"]
            and not args.skip_driver_update
            and result["connectivity_ok"]
        ):
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
