#!/usr/bin/env python3
"""
学术论文搜索下载 MCP Server — IEEE / ScienceDirect / CNKI 统一入口。

通过 CDP 连接用户真实 Chrome/Edge，自动启动浏览器（如未运行）。
用户手动登录一次，cookie 自动保存恢复。

MCP tools (格式: {数据库}_{操作}):
  ieee_login / ieee_search / ieee_detail / ieee_download
  sciencedirect_login / sciencedirect_search / sciencedirect_detail / sciencedirect_download
  cnki_login / cnki_search / cnki_detail / cnki_download
  status / logout

添加新数据库: 编辑 registry.py + 在 databases/ 下创建适配器
"""

import asyncio
import base64
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from carsi_search.engine import CarsiAuth, log
from carsi_search.registry import list_dbs, get_db, get_adapter
from carsi_search.databases.cnki import CnkiAdapter

from playwright.async_api import Page as PwPage

app = Server("cnki-ieee-download")

_auth: CarsiAuth | None = None
_pages: dict = {}
DB_LIST = ", ".join(list_dbs())


# ══════════════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════════════

async def _ensure_connection() -> str | None:
    """Ensure CDP connection exists. Returns error message or None."""
    global _auth, _pages
    if _auth and _auth.context:
        # Verify the connection is actually alive by probing it
        try:
            _ = _auth.context.pages
            return None
        except Exception:
            log.info("[CDP] Connection stale, reconnecting...")
    if _auth:
        try:
            await _auth.stop()
        except Exception:
            pass
    _pages = {}
    _auth = CarsiAuth()
    try:
        await _auth.start()
        return None
    except RuntimeError as e:
        _auth = None
        return str(e)


async def _ensure_page(db: str):
    """Get or create a page for the database. Returns (page, error_or_None)."""
    global _auth, _pages

    err = await _ensure_connection()
    if err:
        return None, err

    ctx = _auth.context  # type: ignore[union-attr]
    if not ctx:
        return None, "CDP 连接已断开"

    page = _pages.get(db)
    if page:
        try:
            _ = page.url
        except Exception:
            page = None
            _pages.pop(db, None)

    if not page:
        db_config = get_db(db)
        if not db_config:
            return None, f"Unknown database: {db}"
        domain = db_config["home_url"].split("/")[2]
        try:
            for p in ctx.pages:
                if domain in p.url and "login" not in p.url.lower():
                    page = p
                    break
        except Exception:
            pass

    if not page:
        try:
            page = await ctx.new_page()
        except Exception as e:
            # Context truly dead — force reconnect
            log.info(f"[CDP] new_page failed, forcing reconnect: {e}")
            _auth = None
            _pages = {}
            err = await _ensure_connection()
            if err:
                return None, err
            ctx = _auth.context  # type: ignore[union-attr]
            if not ctx:
                return None, "CDP 重连失败"
            page = await ctx.new_page()

    _pages[db] = page
    return page, None


def _is_logged_in(db: str, page_text: str) -> bool:
    """Check if user is logged in based on page text keywords."""
    if "Are you a robot" in page_text or "Just a moment" in page_text:
        return False
    if db == "sciencedirect":
        has_inst = "institutional access via" in page_text.lower()
        has_sign_in = "Sign in" in page_text and "Sign in via" not in page_text
        return has_inst or not has_sign_in
    elif db == "cnki":
        return "机构登录" not in page_text and "校外访问" not in page_text
    else:
        return "Institutional Sign In" not in page_text


def _need_login_response(db: str) -> list[TextContent]:
    """Create standardized login-required response with action prompt."""
    db_config = get_db(db)
    label = db_config["label"] if db_config else db
    home = db_config["home_url"] if db_config else ""

    guides = {
        "cnki": (
            "1. 打开已启动的 Chrome/Edge 浏览器\n"
            "2. 访问 https://kns.cnki.net\n"
            "3. 点击「机构登录」→「校外访问」\n"
            "4. 选择学校并完成认证"
        ),
        "sciencedirect": (
            "1. 打开已启动的 Chrome/Edge 浏览器\n"
            "2. 访问 https://www.sciencedirect.com\n"
            "3. 点击 Sign in → Sign in via your institution\n"
            "4. 完成机构认证"
        ),
        "ieee": (
            "1. 打开已启动的 Chrome/Edge 浏览器\n"
            f"2. 访问 {home}\n"
            "3. 点击 Institutional Sign In\n"
            "4. 完成机构认证"
        ),
    }
    steps = guides.get(db, guides["ieee"])

    return [TextContent(type="text",
        text=f"⚠️ 需要登录 {label}\n\n"
             f"请在浏览器中完成以下操作：\n{steps}\n\n"
             f"登录完成后请告知我，我将重试当前操作。\n\n"
             f"[ACTION_REQUIRED: 请使用 AskUserQuestion 询问用户是否已完成 {label} 登录]")]


def _need_action_response(db: str, action: str) -> list[TextContent]:
    """Create standardized action-required response (Cloudflare, captcha)."""
    db_config = get_db(db)
    label = db_config["label"] if db_config else db
    return [TextContent(type="text",
        text=f"⚠️ 需要手动操作\n\n"
             f"{label} {action}\n"
             f"请在浏览器中手动完成，完成后告知我。\n\n"
             f"[ACTION_REQUIRED: 请使用 AskUserQuestion 询问用户是否已完成操作]")]


async def _try_cookie_session(db: str) -> bool:
    """Try to restore session from saved cookies. Returns True on success."""
    global _auth, _pages
    old_auth = _auth

    auth = CarsiAuth()
    try:
        await auth.start()
    except RuntimeError:
        return False

    ctx = auth.context
    if not ctx:
        await auth.stop()
        return False

    db_config = get_db(db)
    if not db_config:
        await auth.stop()
        return False

    domain = db_config["home_url"].split("/")[2]
    page = None
    for p in ctx.pages:
        if domain in p.url and "login" not in p.url.lower():
            page = p
            break
    if not page:
        page = await ctx.new_page()
        await page.goto(db_config["home_url"], wait_until="domcontentloaded", timeout=30000)

    url = page.url
    if any(kw in url.lower() for kw in ["login", "wayf", "cas", "authserver"]):
        await auth.stop()
        return False
    if domain not in url:
        await auth.stop()
        return False

    try:
        page_text = await page.evaluate("() => document.body.innerText.slice(0, 5000)")
        if not _is_logged_in(db, page_text):
            await auth.stop()
            return False
    except Exception as e:
        log.debug(f"Login check error (ignored): {e}")

    if old_auth and old_auth is not auth:
        try:
            await old_auth.stop()
        except Exception as e:
            log.debug(f"Old auth cleanup: {e}")

    _auth = auth
    _pages[db] = page
    return True


async def _verify_login(db: str, page) -> bool:
    """Navigate to db home if needed and verify login status. Returns True if logged in."""
    db_config = get_db(db)
    if not db_config:
        return False

    domain = db_config["home_url"].split("/")[2]

    try:
        current = page.url
    except Exception:
        return False

    if domain not in current:
        try:
            await page.goto(db_config["home_url"], wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return False

    try:
        page_text = await page.evaluate("() => document.body.innerText.slice(0, 5000)")
        return _is_logged_in(db, page_text)
    except Exception:
        return False


async def _ensure_logged_in(db: str) -> tuple[PwPage | None, list[TextContent] | None]:
    """Ensure we have a logged-in page for db. Returns (page, error_response_or_None).

    Flow:
    1. Try existing page → verify login
    2. Try cookie restore → verify login
    3. Return need_login_response
    """
    global _auth, _pages

    # Step 1: if we already have a page, verify it's still logged in
    if _auth and _pages.get(db):
        page = _pages[db]
        try:
            _ = page.url
            if await _verify_login(db, page):
                return page, None
        except Exception:
            pass
        # Page invalid or not logged in — remove it
        _pages.pop(db, None)

    # Step 2: try cookie restore
    if await _try_cookie_session(db):
        page = _pages[db]
        log.info(f"[CDP] {db} session restored from cookies")
        return page, None

    # Step 3: no valid session — prompt user to login
    return None, _need_login_response(db)


async def _save_cookies():
    """Save cookies if auth is active."""
    if _auth:
        try:
            await _auth.save_state()
        except Exception as e:
            log.debug(f"Cookie save error: {e}")


# ══════════════════════════════════════════════════════════════════════
# Tool Definitions
# ══════════════════════════════════════════════════════════════════════

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── IEEE ──
        Tool(
            name="ieee_login",
            description="Connect Chrome via CDP and check IEEE Xplore login status. Auto-launches Chrome if needed.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="ieee_search",
            description="Search IEEE Xplore for papers. Requires prior login via ieee_login.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "page": {"type": "integer", "description": "Page number", "default": 1},
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="ieee_detail",
            description="Get full paper metadata from an IEEE Xplore paper page.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "IEEE paper detail page URL"},
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="ieee_download",
            description="Download a paper PDF from IEEE Xplore. Uses browser JS fetch with CARSI cookies. Saves to downloads/.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "IEEE paper detail URL (or direct PDF URL)"},
                    "title": {"type": "string", "description": "Paper title (used as filename)."},
                },
                "required": ["url"]
            }
        ),
        # ── ScienceDirect ──
        Tool(
            name="sciencedirect_login",
            description="Connect Chrome and check ScienceDirect login status. Cloudflare may require manual verification.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="sciencedirect_search",
            description="Search ScienceDirect for papers. Requires prior login via sciencedirect_login.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "page": {"type": "integer", "description": "Page number", "default": 1},
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="sciencedirect_detail",
            description="Get full paper metadata from a ScienceDirect article page.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "ScienceDirect article URL"},
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="sciencedirect_download",
            description="Download a paper PDF from ScienceDirect. Cloudflare may require manual verification. Saves to downloads/.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "ScienceDirect article URL or pdfft URL"},
                    "title": {"type": "string", "description": "Paper title (used as filename)."},
                },
                "required": ["url"]
            }
        ),
        # ── CNKI ──
        Tool(
            name="cnki_login",
            description="Check CNKI login status. User logs in manually in Chrome; this tool only checks and reports.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="cnki_search",
            description="Search CNKI (中国知网) for papers. Supports advanced filters. Auto-connects Chrome if needed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords (Chinese or English)"},
                    "author": {"type": "string", "description": "Filter by author name"},
                    "journal": {"type": "string", "description": "Filter by journal/source name"},
                    "year_start": {"type": "string", "description": "Start year (e.g. '2020')"},
                    "year_end": {"type": "string", "description": "End year (e.g. '2025')"},
                    "page": {"type": "integer", "description": "Page number (default 1)", "default": 1},
                    "sort": {"type": "string", "description": "Sort: relevance, date, citations, downloads"},
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="cnki_detail",
            description="Get full paper metadata from a CNKI paper detail page.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "CNKI paper detail URL (contains kcms2/article/abstract)"},
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="cnki_download",
            description="Download a paper PDF/CAJ from CNKI. Requires user to be logged in to CNKI in Chrome.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "CNKI paper detail URL"},
                },
                "required": ["url"]
            }
        ),
        # ── Generic ──
        Tool(
            name="status",
            description="Check CDP connection status and list available databases.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="logout",
            description="Disconnect CDP connection. Does NOT close Chrome browser.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
    ]


# ══════════════════════════════════════════════════════════════════════
# Tool Dispatch
# ══════════════════════════════════════════════════════════════════════

async def _dispatch(name: str, args: dict) -> list[TextContent]:
    if name == "ieee_login":             return await handle_login("ieee")
    if name == "ieee_search":            return await handle_search("ieee", args)
    if name == "ieee_detail":            return await handle_detail("ieee", args)
    if name == "ieee_download":          return await handle_download("ieee", args)
    if name == "sciencedirect_login":    return await handle_login("sciencedirect")
    if name == "sciencedirect_search":   return await handle_search("sciencedirect", args)
    if name == "sciencedirect_detail":   return await handle_detail("sciencedirect", args)
    if name == "sciencedirect_download": return await handle_download("sciencedirect", args)
    if name == "cnki_login":             return await handle_login("cnki")
    if name == "cnki_search":            return await handle_cnki_search(args)
    if name == "cnki_detail":            return await handle_cnki_detail(args)
    if name == "cnki_download":          return await handle_cnki_download(args)
    if name == "status":                 return await handle_status()
    if name == "logout":                 return await handle_logout()
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


@app.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    t0 = time.time()
    try:
        result = await _dispatch(name, args)
        elapsed = time.time() - t0
        if result:
            result[0].text += f"\n\n⏱ {elapsed:.1f}s"
        return result
    except Exception as e:
        log.exception(f"Tool {name} error")
        return [TextContent(type="text", text=f"Error in {name}: {e}")]


# ══════════════════════════════════════════════════════════════════════
# Shared Handlers (IEEE / ScienceDirect / CNKI login)
# ══════════════════════════════════════════════════════════════════════

async def handle_login(db: str) -> list[TextContent]:
    """Connect Chrome and check login status for any database."""
    global _pages

    if db not in list_dbs():
        return [TextContent(type="text", text=f"Unknown database: {db}. Available: {DB_LIST}")]

    err = await _ensure_connection()
    if err:
        return [TextContent(type="text", text=err)]

    ctx = _auth.context  # type: ignore[union-attr]
    if not ctx:
        return [TextContent(type="text", text="CDP 连接失败")]

    db_config = get_db(db)
    if not db_config:
        return [TextContent(type="text", text=f"Unknown database: {db}")]

    label = db_config["label"]
    home_url = db_config["home_url"]
    domain = home_url.split("/")[2]

    page = None
    for p in ctx.pages:
        if domain in p.url and "login" not in p.url.lower():
            page = p
            break
    if not page:
        page = await ctx.new_page()
        await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)

    current_url = page.url
    is_on_login = any(kw in current_url.lower() for kw in ["login", "wayf", "cas", "authserver"])

    logged_in = False
    if not is_on_login and domain in current_url:
        try:
            page_text = await page.evaluate("() => document.body.innerText.slice(0, 5000)")
            if "Are you a robot" in page_text or "Just a moment" in page_text:
                return _need_action_response(db, "显示了 Cloudflare 验证页面。")
            logged_in = _is_logged_in(db, page_text)
        except Exception as e:
            log.debug(f"Login check error: {e}")
            logged_in = True

    if logged_in:
        _pages[db] = page
        await _save_cookies()
        return [TextContent(type="text",
            text=f"✅ 已连接 {label}。\n"
                 f"URL: {current_url[:120]}\n"
                 f"Cookie 已保存，下次启动自动恢复。")]
    else:
        return _need_login_response(db)


async def handle_search(db: str, args: dict) -> list[TextContent]:
    """Search papers in IEEE or ScienceDirect."""
    page, err = await _ensure_logged_in(db)
    if err or not page:
        return err or _need_login_response(db)

    adapter = await get_adapter(db, page)
    result = await adapter.search(args["query"], page=args.get("page", 1))

    if not result.get("success"):
        err = result.get("error", "")
        if err == "captcha":
            return _need_action_response(db, "显示了验证页面。")
        return [TextContent(type="text", text=f"Search failed: {err}")]

    papers = result.get("papers", [])
    if not papers:
        return [TextContent(type="text", text="No papers found.")]

    total = result.get("total", "")
    total_str = f" (total: {total})" if total else ""
    page_num = args.get("page", 1)
    text = f"Page {page_num}, {len(papers)} papers{total_str}:\n\n"
    for i, p in enumerate(papers, 1):
        text += f"{i}. **{p.get('title', 'No title')}**\n"
        if p.get('authors'): text += f"   Authors: {p['authors']}\n"
        if p.get('year'): text += f"   Year: {p['year']}\n"
        if p.get('source'): text += f"   Source: {p['source']}\n"
        if p.get('abstract'): text += f"   Abstract: {p['abstract'][:200]}...\n"
        if p.get('url'): text += f"   URL: {p['url']}\n"
        text += "\n"
    text += f"-> Use {db}_detail(url=URL) for full metadata"

    await _save_cookies()
    return [TextContent(type="text", text=text)]


async def handle_detail(db: str, args: dict) -> list[TextContent]:
    """Get paper details from IEEE or ScienceDirect."""
    page, err = await _ensure_logged_in(db)
    if err or not page:
        return err or _need_login_response(db)

    adapter = await get_adapter(db, page)
    result = await adapter.detail(args["url"])

    if not result.get("success"):
        err = result.get("error", "")
        if err == "captcha":
            return _need_action_response(db, "详情页显示了验证页面。")
        return [TextContent(type="text", text=f"Detail failed: {err}")]

    text = ""
    if result.get("abstract"): text += f"**Abstract**\n{result['abstract']}\n\n"
    if result.get("authors"):
        authors = result["authors"] if isinstance(result["authors"], list) else [result["authors"]]
        text += f"**Authors**: {', '.join(authors)}\n"
    if result.get("affiliation"): text += f"**Affiliation**: {result['affiliation']}\n"
    if result.get("year"): text += f"**Year**: {result['year']}\n"
    if result.get("venue"): text += f"**Publication**: {result['venue']}\n"
    if result.get("doi"): text += f"**DOI**: {result['doi']}\n"
    if result.get("keywords"):
        kws = result["keywords"] if isinstance(result["keywords"], list) else [result["keywords"]]
        text += f"**Keywords**: {', '.join(kws)}\n"
    if result.get("volume"): text += f"**Volume**: {result['volume']}\n"
    if result.get("pages"): text += f"**Pages**: {result['pages']}\n"
    if result.get("issn"): text += f"**ISSN**: {result['issn']}\n"
    if result.get("pubDate"): text += f"**Published**: {result['pubDate']}\n"
    if result.get("pdfUrl"):
        text += f"**PDF**: {result['pdfUrl']}\n"
        text += f"**Download**: use {db}_download(url=\"{result['pdfUrl']}\")\n"
    if result.get("citation"): text += f"\n**Citation**\n{result['citation']}\n"

    await _save_cookies()
    return [TextContent(type="text", text=text or "No details extracted.")]


# ══════════════════════════════════════════════════════════════════════
# Download Helpers
# ══════════════════════════════════════════════════════════════════════

async def _resolve_pdf_url(db: str, page, url: str, title: str):
    """Resolve URL to a direct PDF link. Returns (pdf_url, title, error_or_None)."""
    if any(kw in url for kw in ["stamp.jsp", "/pdf/", "getPDF.jsp", "pdfft"]):
        return url, title, None

    adapter = await get_adapter(db, page)
    detail_result = await adapter.detail(url)
    if detail_result.get("error") == "captcha":
        return url, title, "captcha"
    if detail_result.get("pdfUrl"):
        url = detail_result["pdfUrl"]
    if not title and detail_result.get("title"):
        title = detail_result["title"]
    return url, title, None


def _ieee_pdf_url(url: str) -> str:
    """Convert IEEE document/stamp URLs to getPDF.jsp endpoint."""
    if "/document/" in url and "getPDF" not in url:
        m = re.search(r'/document/(\d+)', url)
        if m:
            return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}"
    if "stamp.jsp" in url:
        arnumber = url.split("arnumber=")[-1] if "arnumber=" in url else ""
        if arnumber:
            return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"
    return url


_FETCH_PDF_JS = """
    async (targetUrl) => {
        try {
            const resp = await fetch(targetUrl);
            if (!resp.ok) return 'HTTP ' + resp.status;
            const buf = await resp.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
            return btoa(binary);
        } catch(e) {
            return 'ERROR:' + e.message;
        }
    }
"""

_FETCH_CURRENT_PAGE_JS = """
    async () => {
        try {
            const resp = await fetch(window.location.href);
            if (!resp.ok) return 'HTTP ' + resp.status;
            const buf = await resp.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
            return btoa(binary);
        } catch(e) {
            return 'ERROR:' + e.message;
        }
    }
"""


async def _sd_navigate_and_fetch(page, url: str) -> str | None:
    """Navigate to ScienceDirect PDF URL, handle Cloudflare, return base64 or None.

    Cloudflare verification can destroy the execution context. After user completes
    the challenge manually, the page reloads — we wait and retry evaluate.
    """
    try:
        await page.unroute("**/*")
    except Exception:
        pass

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    for _ in range(20):
        await asyncio.sleep(1)
        if "sciencedirectassets" in page.url:
            break

    # Check for Cloudflare bot challenge — wait for user to complete it
    for attempt in range(60):
        try:
            page_text = await page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''")
        except Exception:
            # Context destroyed (Cloudflare verification caused navigation)
            await asyncio.sleep(2)
            continue

        if "robot" not in page_text.lower() and "just a moment" not in page_text.lower():
            break
        await asyncio.sleep(3)
    else:
        return None

    # Wait for page to stabilize after Cloudflare pass
    await asyncio.sleep(2)

    # Retry evaluate with resilience to context destruction
    for retry in range(3):
        try:
            return await page.evaluate(_FETCH_CURRENT_PAGE_JS)
        except Exception as e:
            if retry < 2:
                log.debug(f"SD fetch retry {retry+1}: {e}")
                await asyncio.sleep(2)
            else:
                log.info(f"SD fetch failed after retries: {e}")
                return None


def _save_pdf(pdf_data: bytes, title: str) -> Path:
    """Save PDF bytes to downloads directory."""
    downloads_dir = Path(os.getcwd()) / "downloads"
    downloads_dir.mkdir(exist_ok=True)
    if title:
        safe_title = re.sub(r'[<>:"/\\|?*]', '', title).strip()[:80]
        filename = f"{safe_title}.pdf"
    else:
        filename = f"paper_{int(time.time())}.pdf"
    save_path = downloads_dir / filename
    save_path.write_bytes(pdf_data)
    return save_path


# ══════════════════════════════════════════════════════════════════════
# Download Handler (IEEE / ScienceDirect)
# ══════════════════════════════════════════════════════════════════════

async def handle_download(db: str, args: dict) -> list[TextContent]:
    """Download PDF from IEEE or ScienceDirect."""
    page, err = await _ensure_logged_in(db)
    if err or not page:
        return err or _need_login_response(db)

    url = args["url"]
    title = args.get("title", "")

    url, title, resolve_err = await _resolve_pdf_url(db, page, url, title)
    if resolve_err == "captcha":
        return _need_action_response(db, "详情页显示了验证页面。")

    if db == "ieee":
        url = _ieee_pdf_url(url)

    is_sd = "sciencedirect" in url or "sciencedirectassets" in page.url
    if is_sd and "/pdfft" in url:
        pdf_b64 = await _sd_navigate_and_fetch(page, url)
        if pdf_b64 is None:
            return _need_action_response(db, "PDF 域名显示了 Cloudflare 验证。")
    else:
        try:
            await page.unroute("**/*")
        except Exception:
            pass
        pdf_b64 = await page.evaluate(_FETCH_PDF_JS, url)

    if not pdf_b64 or pdf_b64.startswith('ERROR:') or pdf_b64.startswith('HTTP '):
        await page.goto(
            url.replace('getPDF.jsp', 'stamp.jsp'),
            wait_until="domcontentloaded", timeout=45000,
        )
        return [TextContent(type="text",
            text=f"自动下载失败 ({pdf_b64[:80] if pdf_b64 else 'empty'})。已在浏览器中打开。\n"
                 f"URL: {page.url[:200]}")]

    pdf_data = base64.b64decode(pdf_b64)
    if pdf_data[:4] != b'%PDF':
        snippet = pdf_data[:200].decode('utf-8', errors='replace')
        await page.goto(
            url.replace('getPDF.jsp', 'stamp.jsp'),
            wait_until="domcontentloaded", timeout=45000,
        )
        return [TextContent(type="text",
            text=f"下载失败：返回内容不是 PDF（可能登录过期）。\n"
                 f"已在浏览器中打开。\nFirst bytes: {snippet[:100]}")]

    save_path = _save_pdf(pdf_data, title)
    await _save_cookies()
    return [TextContent(type="text",
        text=f"Downloaded PDF ({len(pdf_data)} bytes)\nSaved: {save_path}")]


# ══════════════════════════════════════════════════════════════════════
# Generic Handlers
# ══════════════════════════════════════════════════════════════════════

async def handle_status() -> list[TextContent]:
    lines = [f"**Registered databases**: {DB_LIST}"]
    for name in list_dbs():
        db = get_db(name)
        if not db:
            continue
        logged_in = name in _pages
        marker = " ← logged in" if logged_in else ""
        lines.append(f"  - `{name}`: {db['label']}{marker}")

    if _auth and _auth.context:
        lines.append("\nCDP 连接: 已连接")
        for db_name, pg in _pages.items():
            try:
                lines.append(f"  {db_name}: {pg.url[:80]}")
            except Exception:
                lines.append(f"  {db_name}: (page invalid)")
    else:
        lines.append("\nCDP 连接: 未连接。使用 {db}_login 工具连接。")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_logout() -> list[TextContent]:
    global _auth, _pages
    if _auth:
        await _auth.clear_state()
        try:
            await _auth.stop()
        except Exception as e:
            log.debug(f"Logout cleanup: {e}")
    _auth = None
    _pages = {}
    return [TextContent(type="text", text="已断开 CDP 连接。浏览器保持打开。")]


# ══════════════════════════════════════════════════════════════════════
# CNKI Handlers
# ══════════════════════════════════════════════════════════════════════

async def handle_cnki_search(args: dict) -> list[TextContent]:
    page, err = await _ensure_page("cnki")
    if err or not page:
        return [TextContent(type="text", text=err or "Failed to get CNKI page")]

    adapter = CnkiAdapter(page)
    result = await adapter.search(
        args["query"],
        author=args.get("author"),
        journal=args.get("journal"),
        year_start=args.get("year_start"),
        year_end=args.get("year_end"),
        page=args.get("page", 1),
        sort=args.get("sort"),
    )

    if not result.get("success"):
        err_msg = result.get("error", "unknown")
        if err_msg == "captcha":
            return _need_action_response("cnki", "正在显示滑块验证码。")
        return [TextContent(type="text", text=f"CNKI search failed: {err_msg}")]

    papers = result.get("papers", [])
    total = result.get("total", "?")
    page_info = result.get("page", "1/1")
    text = f"CNKI 搜索 \"{args['query']}\"：共 {total} 条结果 (第 {page_info} 页)\n\n"
    for i, p in enumerate(papers):
        text += f"[{i+1}] **{p.get('title', '?')}**\n"
        if p.get("authors"): text += f"    作者: {p['authors']}\n"
        if p.get("journal"): text += f"    期刊: {p['journal']}\n"
        if p.get("date"): text += f"    日期: {p['date']}\n"
        if p.get("citations"): text += f"    引用: {p['citations']}\n"
        if p.get("url"): text += f"    URL: {p['url']}\n"
        text += "\n"
    text += "→ 使用 cnki_detail(url=URL) 获取论文详情"
    return [TextContent(type="text", text=text)]


async def handle_cnki_detail(args: dict) -> list[TextContent]:
    page, err = await _ensure_page("cnki")
    if err or not page:
        return [TextContent(type="text", text=err or "Failed to get CNKI page")]

    adapter = CnkiAdapter(page)
    result = await adapter.detail(args["url"])

    if not result.get("success"):
        err_msg = result.get("error", "unknown")
        if err_msg == "captcha":
            return _need_action_response("cnki", "验证码。")
        return [TextContent(type="text", text=f"CNKI detail failed: {err_msg}")]

    text = ""
    if result.get("title"): text += f"**{result['title']}**\n\n"
    if result.get("authors"): text += f"**作者**: {', '.join(result['authors'])}\n"
    if result.get("affiliations"): text += f"**单位**: {', '.join(result['affiliations'])}\n"
    if result.get("journal"): text += f"**期刊**: {result['journal']}\n"
    if result.get("pubInfo"): text += f"**出版信息**: {result['pubInfo']}\n"
    if result.get("doi"): text += f"**DOI**: {result['doi']}\n"
    if result.get("abstract"): text += f"\n**摘要**\n{result['abstract']}\n"
    if result.get("keywords"): text += f"\n**关键词**: {', '.join(result['keywords'])}\n"
    if result.get("fund"): text += f"**基金**: {result['fund']}\n"
    if result.get("classification"): text += f"**分类号**: {result['classification']}\n"
    if result.get("isOnlineFirst"): text += "**状态**: 网络首发\n"
    return [TextContent(type="text", text=text or "未提取到详情")]


async def handle_cnki_download(args: dict) -> list[TextContent]:
    """CNKI download using CDP setDownloadBehavior.

    In CDP (connect_over_cdp) mode, Playwright's download.save_as() / download.path()
    often produce 0-byte files because Playwright can't access the browser's temp files.
    Instead, we use CDP's Browser.setDownloadBehavior to tell the browser to save directly
    to our target directory, then poll for the file to appear.
    """
    page, err = await _ensure_page("cnki")
    if err or not page:
        return [TextContent(type="text", text=err or "Failed to get CNKI page")]

    url = args["url"]
    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    try:
        await page.wait_for_selector('.brief h1', timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(1)

    page_text = await page.evaluate("() => document.body.innerText.slice(0, 3000)")
    if "机构登录" in page_text or "校外访问" in page_text:
        return _need_login_response("cnki")

    captcha = await page.evaluate("""() => {
        const el = document.querySelector('#tcaptcha_transform_dy');
        return el && el.getBoundingClientRect().top >= 0;
    }""")
    if captcha:
        return _need_action_response("cnki", "正在显示滑块验证码。")

    has_pdf = await page.evaluate("() => !!document.querySelector('#pdfDown, .btn-dlpdf a')")
    if not has_pdf:
        return [TextContent(type="text", text="未找到下载链接。")]

    save_dir = Path(os.getcwd()) / "downloads"
    save_dir.mkdir(exist_ok=True)

    # Record existing files before download
    existing_files = set(save_dir.iterdir())

    # Use CDP to redirect browser downloads to our target directory
    cdp = await page.context.new_cdp_session(page)
    try:
        await cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allowAndName",
            "downloadPath": str(save_dir.resolve()),
            "eventsEnabled": True,
        })
    except Exception as e:
        log.debug(f"CDP setDownloadBehavior failed, trying Page-level: {e}")
        try:
            await cdp.send("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(save_dir.resolve()),
            })
        except Exception as e2:
            log.info(f"CDP download redirect failed: {e2}")

    # Click download button
    await page.locator('#pdfDown, .btn-dlpdf a').first.click()

    # Wait for new file to appear in save_dir (poll up to 60s)
    new_file = None
    for _ in range(60):
        await asyncio.sleep(1)
        current_files = set(save_dir.iterdir())
        new_files = current_files - existing_files
        # Filter out partial downloads (.crdownload, .tmp, .part)
        completed = [
            f for f in new_files
            if not f.suffix.lower() in ('.crdownload', '.tmp', '.part', '.download')
            and f.stat().st_size > 0
        ]
        if completed:
            new_file = completed[0]
            # Wait for file to finish writing (size stabilizes)
            prev_size = new_file.stat().st_size
            await asyncio.sleep(1)
            curr_size = new_file.stat().st_size
            if curr_size == prev_size and curr_size > 0:
                break
            # Still writing, keep waiting
            new_file = None

    # Cleanup CDP session
    try:
        await cdp.detach()
    except Exception:
        pass

    if not new_file:
        return [TextContent(type="text",
            text="⚠️ CNKI 下载超时。\n"
                 "文件可能已下载到浏览器默认目录。请检查浏览器下载记录。\n\n"
                 f"[ACTION_REQUIRED: 请使用 AskUserQuestion 询问用户是否已登录 CNKI]")]

    file_size = new_file.stat().st_size
    with open(new_file, 'rb') as f:
        header = f.read(4)
    if header != b'%PDF':
        content = new_file.read_bytes()[:200].decode('utf-8', errors='replace')
        new_file.unlink()
        return [TextContent(type="text",
            text=f"CNKI 下载失败：返回的不是 PDF 文件。\n内容预览: {content[:100]}\n\n"
                 f"[ACTION_REQUIRED: 请使用 AskUserQuestion 询问用户是否已登录 CNKI]")]

    # Rename UUID file to paper title
    try:
        title = await page.evaluate(
            "() => (document.querySelector('.brief h1')?.innerText || '')"
            "  .replace(/\\s*附视频\\s*$/, '').replace(/\\s*网络首发\\s*$/, '').trim()"
        )
        if title:
            safe_title = re.sub(r'[<>:"/\\|?*]', '', title).strip()[:80]
            ext = new_file.suffix if new_file.suffix else '.pdf'
            renamed = new_file.parent / f"{safe_title}{ext}"
            if renamed.exists():
                renamed = new_file.parent / f"{safe_title}_{int(time.time())}{ext}"
            new_file.rename(renamed)
            new_file = renamed
    except Exception as e:
        log.debug(f"Rename failed (keeping UUID name): {e}")

    await _save_cookies()
    return [TextContent(type="text",
        text=f"CNKI PDF 下载成功：{new_file.name}\n大小: {file_size} bytes\n保存: {new_file}")]


# ══════════════════════════════════════════════════════════════════════
# MCP Server Entry
# ══════════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
