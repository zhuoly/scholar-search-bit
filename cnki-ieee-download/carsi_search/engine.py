"""
CDP connection manager for Chrome/Edge browser with cookie persistence.

Auto-launches Chrome or Edge with CDP debugging if not already running.
Saves/loads cookies so the user only needs to log in once.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Browser

CDP_URL = os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222")
_CDP_PROFILE = Path.home() / ".carsi_chrome_profile"

LOG_FILE = Path(__file__).parent.parent / "carsi.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("carsi")


def _find_browser() -> str | None:
    """Find Chrome or Edge executable on the system."""
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    if sys.platform == "win32":
        candidates = [
            # Edge (preferred on Windows)
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            # Chrome
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = []

    for p in candidates:
        if Path(p).exists():
            return p

    # Fallback: search PATH
    for name in ["msedge", "microsoft-edge", "chrome", "google-chrome", "chromium"]:
        found = shutil.which(name)
        if found:
            return found

    return None


def _is_cdp_available() -> bool:
    """Quick check if CDP port is already listening."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


class CarsiAuth:
    """CDP connection wrapper with auto-launch and cookie save/restore."""

    STATE_FILE = Path(__file__).parent.parent / ".carsi_state.json"

    def __init__(self):
        self.browser: Browser | None = None
        self.context = None
        self._playwright = None
        self._browser_process: subprocess.Popen | None = None

    async def start(self):
        if not _is_cdp_available():
            await self._launch_browser()

        self._playwright = await async_playwright().start()
        for attempt in range(3):
            try:
                self.browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    await self._playwright.stop()
                    self._playwright = None
                    raise RuntimeError(
                        f"无法连接浏览器 CDP ({CDP_URL})。"
                        f"请手动启动 Chrome/Edge: msedge --remote-debugging-port=9222"
                    )

        if not self.browser:
            raise RuntimeError("浏览器连接失败")

        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        log.info(f"[CDP] 已连接浏览器: {CDP_URL}")

        await self._restore_cookies()
        return self

    async def _launch_browser(self):
        browser_path = _find_browser()
        if not browser_path:
            raise RuntimeError(
                "找不到 Chrome/Edge 浏览器。请安装 Chrome 或 Edge，或设置 CHROME_PATH 环境变量。"
            )

        browser_name = "Edge" if "edge" in browser_path.lower() else "Chrome"
        log.info(f"[CDP] 自动启动 {browser_name}...")
        _CDP_PROFILE.mkdir(parents=True, exist_ok=True)

        port = CDP_URL.split(":")[-1].rstrip("/")
        cmd = [
            browser_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={_CDP_PROFILE}",
        ]

        try:
            self._browser_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"[CDP] {browser_name} 已启动 (PID={self._browser_process.pid})")
            await asyncio.sleep(2)
        except Exception as e:
            raise RuntimeError(f"启动 {browser_name} 失败: {e}")

    async def save_state(self):
        if not self.context:
            return
        try:
            state = await self.context.storage_state()
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            cookie_count = len(state.get("cookies", []))
            log.info(f"[CDP] 已保存 {cookie_count} 个 cookie")
        except Exception as e:
            log.info(f"[CDP] 保存 cookie 失败: {e}")

    async def _restore_cookies(self):
        if not self.STATE_FILE.exists() or self.STATE_FILE.stat().st_size <= 50:
            return
        try:
            state = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.info(f"[CDP] 读取 cookie 文件失败: {e}")
            return

        cookies = state.get("cookies", [])
        if not cookies or not self.context:
            return

        # Batch cookie injection
        cdp_cookies = []
        for cookie in cookies:
            cdp_cookie: dict = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie.get("domain", ""),
                "path": cookie.get("path", "/"),
            }
            if cookie.get("secure"):
                cdp_cookie["secure"] = True
            if cookie.get("httpOnly"):
                cdp_cookie["httpOnly"] = True
            if cookie.get("sameSite"):
                cdp_cookie["sameSite"] = cookie["sameSite"]
            if cookie.get("expires", -1) > 0:
                cdp_cookie["expires"] = cookie["expires"]
            cdp_cookies.append(cdp_cookie)

        try:
            await self.context.add_cookies(cdp_cookies)  # type: ignore[arg-type]
            log.info(f"[CDP] 已批量恢复 {len(cdp_cookies)} 个 cookie")
        except Exception as e:
            log.info(f"[CDP] 批量 cookie 注入失败，逐个重试: {e}")
            injected = 0
            for c in cdp_cookies:
                try:
                    await self.context.add_cookies([c])  # type: ignore[arg-type]
                    injected += 1
                except Exception:
                    pass
            log.info(f"[CDP] 逐个恢复 {injected}/{len(cdp_cookies)} 个 cookie")

        # Restore localStorage
        for origin_data in state.get("origins", []):
            origin = origin_data.get("origin", "")
            ls_items = origin_data.get("localStorage", [])
            if not origin or not ls_items:
                continue
            for item in ls_items:
                try:
                    key = item["name"].replace("'", "\\'")
                    val = item["value"].replace("\\", "\\\\").replace("'", "\\'")
                    for page in self.context.pages:
                        if origin in page.url:
                            await page.evaluate(f"localStorage.setItem('{key}', '{val}')")
                            break
                except Exception:
                    pass

    async def clear_state(self):
        if self.STATE_FILE.exists():
            self.STATE_FILE.unlink()
            log.info("[CDP] 已清除保存的 cookie 文件")

    async def stop(self):
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self.browser = None
        self.context = None
        if self._browser_process and self._browser_process.poll() is None:
            self._browser_process.terminate()
            log.info("[CDP] 已关闭自动启动的浏览器")
            self._browser_process = None
