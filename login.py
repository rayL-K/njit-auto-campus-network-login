# -*- coding: utf-8 -*-
"""
Campus network auto-login script.

Primary flow:
1. Reconnect the saved Wi-Fi profile if the campus portal is unreachable.
2. Query portal status through /drcom/chkstatus.
3. Authenticate directly through /drcom/login without opening Chrome.
4. Verify that the expected account is online and, when possible, confirm
   external connectivity.
5. Show Windows notification-area popups for start, success, and failures.

Selenium is kept only as a last-resort fallback. ChromeDriver maintenance is
handled after the network is online so the main login path is no longer blocked
by offline driver checks.
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

POWERSHELL_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
Add-Type -AssemblyName System.Security

$title = [System.Security.SecurityElement]::Escape($env:CAMPUS_LOGIN_TITLE)
$message = [System.Security.SecurityElement]::Escape($env:CAMPUS_LOGIN_MESSAGE)
$appId = if ($env:CAMPUS_LOGIN_APP_ID) { $env:CAMPUS_LOGIN_APP_ID } else { 'PowerShell' }
$duration = if ($env:CAMPUS_LOGIN_DURATION -eq 'long') { 'long' } else { 'short' }

$toastXml = @"
<toast duration="$duration">
  <visual>
    <binding template="ToastGeneric">
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

$systemIcon = switch ($iconName) {
    'Error'   { [System.Drawing.SystemIcons]::Error }
    'Warning' { [System.Drawing.SystemIcons]::Warning }
    default   { [System.Drawing.SystemIcons]::Information }
}

$balloonIcon = switch ($iconName) {
    'Error'   { [System.Windows.Forms.ToolTipIcon]::Error }
    'Warning' { [System.Windows.Forms.ToolTipIcon]::Warning }
    default   { [System.Windows.Forms.ToolTipIcon]::Info }
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $systemIcon
$notify.Visible = $true
$notify.BalloonTipTitle = $title
$notify.BalloonTipText = $message
$notify.BalloonTipIcon = $balloonIcon
$notify.ShowBalloonTip($timeout)
Start-Sleep -Milliseconds ($timeout + 1000)
$notify.Dispose()
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
    """Base exception for login failures."""


class RetryableLoginError(LoginError):
    """A failure that can reasonably be retried."""


class NonRetryableLoginError(LoginError):
    """A failure that should stop immediately."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Campus portal auto-login.")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only query and print the current campus portal status.",
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        help="Show a Windows notification test popup and exit.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable Windows notifications for this run.",
    )
    parser.add_argument(
        "--skip-driver-update",
        action="store_true",
        help="Skip best-effort ChromeDriver cache maintenance after success.",
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

    logging.info("Log file: %s", log_path)


def read_json_with_fallbacks(path: Path) -> dict[str, Any]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with path.open("r", encoding=encoding) as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = exc
    raise NonRetryableLoginError(f"Failed to load {path.name}: {last_error}")


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
        raise NonRetryableLoginError("data.json must contain non-empty 'id' and 'password'.")

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
        "enable_browser_fallback": as_bool(raw.get("enable_browser_fallback"), True),
        "connectivity_checks": checks,
        "max_runtime_seconds": max_runtime_seconds,
        "retry_interval_seconds": retry_interval_seconds,
        "wifi_attempts": wifi_attempts,
    }


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "CampusAutoLogin/2.0"})
    return session


def mask_account(account: str) -> str:
    if not account:
        return "<unknown>"
    if "@" in account:
        user, suffix = account.split("@", 1)
        return f"{user[:3]}***@{suffix}"
    return f"{account[:3]}***"


def send_notification(title: str, message: str, enabled: bool = True, icon: str = "Info") -> None:
    if not enabled:
        return
    if os.environ.get("USERNAME", "").upper() == "SYSTEM":
        logging.warning("Skipping Windows notification because the task is running as SYSTEM.")
        return

    env = os.environ.copy()
    env["CAMPUS_LOGIN_TITLE"] = title[:64]
    env["CAMPUS_LOGIN_MESSAGE"] = message.replace("\r", " ").replace("\n", " ")[:240]
    env["CAMPUS_LOGIN_TIMEOUT"] = "5000"
    env["CAMPUS_LOGIN_ICON"] = icon
    env["CAMPUS_LOGIN_APP_ID"] = "PowerShell"
    env["CAMPUS_LOGIN_DURATION"] = "short"

    try:
        toast_result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_TOAST_SCRIPT],
            capture_output=True,
            text=True,
            timeout=12,
            env=env,
        )
        if toast_result.returncode == 0:
            return

        logging.warning("Toast notification failed, falling back to balloon tip: %s", (toast_result.stderr or toast_result.stdout).strip())
        balloon_result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_BALLOON_NOTIFY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=12,
            env=env,
        )
        if balloon_result.returncode != 0:
            logging.warning("Windows balloon notification failed: %s", (balloon_result.stderr or balloon_result.stdout).strip())
    except Exception as exc:
        logging.warning("Windows notification error: %s", exc)


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


def connect_wifi(profile_name: str, session: requests.Session, portal_root: str, attempts: int) -> bool:
    if portal_is_reachable(session, portal_root):
        logging.info("Campus portal is already reachable. Skipping Wi-Fi reconnect.")
        return True

    for attempt in range(1, attempts + 1):
        logging.info("Connecting to Wi-Fi profile %s (attempt %s/%s)...", profile_name, attempt, attempts)
        try:
            result = run_command(["netsh", "wlan", "connect", f"name={profile_name}"], timeout=30)
        except subprocess.TimeoutExpired:
            logging.warning("Wi-Fi connect command timed out.")
            result = None

        if result:
            output = " ".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
            if output:
                logging.info("netsh output: %s", output)

        if wait_for_portal(session, portal_root, timeout_seconds=20):
            logging.info("Campus portal became reachable after Wi-Fi reconnect.")
            return True

        time.sleep(3)

    return False


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
        raise RetryableLoginError(f"Unexpected JSONP response: {stripped[:160]}")

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RetryableLoginError(f"Failed to parse portal response: {exc}") from exc


def fetch_portal_html(session: requests.Session, portal_root: str) -> str:
    try:
        response = session.get(f"{portal_root}/", timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise RetryableLoginError(f"Campus portal homepage is unreachable: {exc}") from exc


def check_portal_status(session: requests.Session, portal_root: str) -> dict[str, Any]:
    try:
        response = session.get(
            f"{portal_root}/drcom/chkstatus",
            params={"callback": "campusStatus", "jsVersion": "4.X"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RetryableLoginError(f"Failed to query portal status: {exc}") from exc

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
        raise RetryableLoginError("Portal page did not expose the current device IP.")

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

    logging.info("Attempting direct portal login for %s.", mask_account(account))

    try:
        response = session.get(
            f"{config['portal_root']}/drcom/login",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RetryableLoginError(f"Direct portal login request failed: {exc}") from exc

    payload = parse_jsonp_payload(response.text)
    if portal_result_is_online(payload):
        return account, payload

    description = describe_login_failure(payload)
    if is_invalid_credentials_error(payload):
        raise NonRetryableLoginError(f"Portal rejected the credentials: {description}")
    raise RetryableLoginError(f"Portal login did not succeed: {description}")


def check_external_connectivity(session: requests.Session, checks: list[dict[str, str]]) -> tuple[bool, str]:
    for check in checks:
        url = check["url"]
        keyword = check.get("keyword", "")
        try:
            response = session.get(url, timeout=6, allow_redirects=True)
        except requests.RequestException:
            continue

        if not 200 <= response.status_code < 400:
            continue
        if keyword and keyword not in response.text[:300]:
            continue
        return True, url

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
                logging.info("Chrome browser major version: %s", match.group(1))
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
    logging.info("Fetching Chrome-for-Testing metadata for Chrome %s...", chrome_major_version)
    try:
        response = requests.get(CHROME_FOR_TESTING_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logging.warning("Failed to fetch ChromeDriver metadata: %s", exc)
        return False

    matching_version = None
    for version in payload.get("versions", []):
        if str(version.get("version", "")).startswith(f"{chrome_major_version}."):
            matching_version = version
            break

    if not matching_version:
        logging.warning("No matching ChromeDriver release found for Chrome %s.", chrome_major_version)
        return False

    download_url = ""
    for item in matching_version.get("downloads", {}).get("chromedriver", []):
        if item.get("platform") == "win64":
            download_url = item.get("url", "")
            break

    if not download_url:
        logging.warning("No win64 ChromeDriver download was provided by Chrome-for-Testing.")
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

        logging.info("ChromeDriver cache updated to %s.", matching_version["version"])
        return True
    except Exception as exc:
        logging.warning("ChromeDriver download/update failed: %s", exc)
        return False
    finally:
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)


def maintain_local_chromedriver() -> None:
    chrome_version = get_chrome_version()
    if not chrome_version:
        logging.info("Chrome was not detected locally. Skipping ChromeDriver maintenance.")
        return

    driver_version = get_local_chromedriver_version()
    if driver_version == chrome_version:
        logging.info("ChromeDriver cache already matches local Chrome.")
        return

    logging.info("ChromeDriver cache mismatch detected: Chrome=%s ChromeDriver=%s", chrome_version, driver_version)
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
            logging.info("Browser fallback did not find a login form. Portal may already be online.")
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
            raise RetryableLoginError("Browser fallback could not find the password field.")

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
            raise RetryableLoginError("Browser fallback could not find the login button.")

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
                f"Campus portal is still unreachable after reconnecting Wi-Fi profile {config['wifi_profile']}."
            )

    html = fetch_portal_html(session, config["portal_root"])
    status = check_portal_status(session, config["portal_root"])
    expected_account = build_login_account(config, infer_account_suffix(config, html, status))
    current_account = current_portal_account(status)

    if portal_result_is_online(status) and account_matches_expected(config, current_account, expected_account):
        connectivity_ok, checked_url = check_external_connectivity(session, config["connectivity_checks"])
        return {
            "account": current_account or expected_account,
            "already_online": True,
            "used_browser_fallback": False,
            "connectivity_ok": connectivity_ok,
            "connectivity_url": checked_url,
        }

    if portal_result_is_online(status) and current_account:
        logging.warning(
            "Portal reports another session is online: %s. Trying to replace it with %s.",
            current_account,
            expected_account,
        )

    account, _ = login_via_http(session, config, html, status)
    time.sleep(3)

    verified_status = check_portal_status(session, config["portal_root"])
    verified_account = current_portal_account(verified_status)
    if not portal_result_is_online(verified_status):
        raise RetryableLoginError("Portal still reports offline after a direct login attempt.")
    if not account_matches_expected(config, verified_account, account):
        raise RetryableLoginError(
            f"Portal came online as {verified_account or '<unknown>'}, not as the expected account."
        )

    connectivity_ok, checked_url = check_external_connectivity(session, config["connectivity_checks"])
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
        logging.info("Login attempt %s started.", attempt)
        try:
            return try_login_once(session, config)
        except NonRetryableLoginError:
            raise
        except RetryableLoginError as exc:
            last_error = exc
            logging.warning("Attempt %s failed: %s", attempt, exc)

        if time.time() + config["retry_interval_seconds"] >= deadline:
            break
        time.sleep(config["retry_interval_seconds"])

    if config["enable_browser_fallback"]:
        logging.warning("Direct HTTP login did not complete successfully. Trying browser fallback.")
        send_notification(
            "校园网自动登录",
            "HTTP 登录未成功，正在尝试浏览器兜底。",
            enabled=notify_enabled,
            icon="Warning",
        )

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
            raise last_error or RetryableLoginError("Browser fallback finished, but the portal still reports offline.")
        if not account_matches_expected(config, verified_account, expected_account):
            raise last_error or RetryableLoginError(
                f"Browser fallback did not bring up the expected account: {verified_account or '<unknown>'}."
            )

        connectivity_ok, checked_url = check_external_connectivity(session, config["connectivity_checks"])
        return {
            "account": verified_account or expected_account,
            "already_online": False,
            "used_browser_fallback": True,
            "connectivity_ok": connectivity_ok,
            "connectivity_url": checked_url,
        }

    raise last_error or RetryableLoginError("Campus network login did not succeed before the retry window ended.")


def show_status(session: requests.Session, config: dict[str, Any]) -> int:
    if not portal_is_reachable(session, config["portal_root"]):
        logging.info("Campus portal is unreachable. Wi-Fi may be disconnected or not on campus network.")
        return 2

    html = fetch_portal_html(session, config["portal_root"])
    status = check_portal_status(session, config["portal_root"])
    expected_account = build_login_account(config, infer_account_suffix(config, html, status))
    current_account = current_portal_account(status) or "<offline>"
    online = portal_result_is_online(status)
    matches = account_matches_expected(config, current_account, expected_account)

    logging.info("Portal online: %s", online)
    logging.info("Current account: %s", current_account)
    logging.info("Expected account: %s", expected_account)
    logging.info("Matches expectation: %s", matches)
    logging.info("Current IPv4: %s", choose_v4ip(status, html) or "<unknown>")
    return 0 if online else 1


def main() -> int:
    args = parse_args()
    setup_logging()
    config = load_config()
    notify_enabled = config["notify"] and not args.no_notify
    session = make_session()

    if args.notify_test:
        send_notification("校园网自动登录", "这是一条测试通知。", enabled=notify_enabled)
        logging.info("Notification test completed.")
        return 0

    if args.status:
        return show_status(session, config)

    send_notification(
        "校园网自动登录",
        "任务已启动，正在检查 Wi-Fi 和校园网认证状态。",
        enabled=notify_enabled,
    )

    try:
        result = run_login_flow(session, config, notify_enabled)
    except NonRetryableLoginError as exc:
        logging.error("%s", exc)
        send_notification("校园网自动登录失败", str(exc), enabled=notify_enabled, icon="Error")
        return 3
    except RetryableLoginError as exc:
        logging.error("%s", exc)
        send_notification("校园网自动登录失败", str(exc), enabled=notify_enabled, icon="Error")
        return 2
    except Exception as exc:
        logging.exception("Unexpected failure")
        send_notification("校园网自动登录异常", str(exc), enabled=notify_enabled, icon="Error")
        return 4

    account_text = result["account"]
    if result["already_online"]:
        message = f"校园网已在线，账号 {account_text} 无需重复认证。"
    elif result["used_browser_fallback"]:
        message = f"校园网已恢复联网，账号 {account_text}，使用了浏览器兜底。"
    else:
        message = f"校园网已恢复联网，账号 {account_text}，通过 HTTP 直接认证成功。"

    if result["connectivity_ok"]:
        message = f"{message} 外网连通性已确认。"
        icon = "Info"
    else:
        message = f"{message} 但外网连通性未完全确认。"
        icon = "Warning"

    logging.info(message)
    send_notification("校园网自动登录成功", message, enabled=notify_enabled, icon=icon)

    if config["post_login_driver_update"] and not args.skip_driver_update and result["connectivity_ok"]:
        try:
            maintain_local_chromedriver()
        except Exception as exc:
            logging.warning("ChromeDriver maintenance skipped due to error: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
