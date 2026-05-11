import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config import BROWSER_ACTION_TIMEOUT, WORKSPACE_ROOT
from core.events import EventBus, make_event

# URL policy — block SSRF vectors
_BLOCKED_SCHEMES = ("file://", "data:", "chrome://", "javascript:")
_BLOCKED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _check_url_policy(url: str):
    for scheme in _BLOCKED_SCHEMES:
        if url.lower().startswith(scheme):
            raise ValueError(f"URL scheme blocked by policy: {scheme}")
    # Basic SSRF: block internal hosts unless explicitly allowed
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
        if host in _BLOCKED_HOSTS:
            raise ValueError(f"URL host blocked by policy: {host}")
    except Exception as e:
        if "blocked" in str(e):
            raise


def resolve_workspace_path(path: str):
    from pathlib import Path
    target = (WORKSPACE_ROOT / path).resolve()
    if WORKSPACE_ROOT not in target.parents and target != WORKSPACE_ROOT:
        raise PermissionError("Path traversal blocked")
    return target


class BrowserSessionManager:
    def __init__(self):
        self.sessions: Dict[str, Any] = {}
        self.playwright = None
        self.browser = None

    async def init_playwright(self):
        if self.playwright is None:
            try:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(headless=True)
            except ImportError:
                raise RuntimeError("Install: pip install playwright && playwright install chromium")

    async def create_session(self) -> str:
        await self.init_playwright()
        context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        session_id = f"brs_{uuid.uuid4().hex[:12]}"
        self.sessions[session_id] = {
            "context": context,
            "page": page,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return session_id

    async def get_page(self, session_id: str):
        if session_id not in self.sessions:
            raise ValueError(f"Browser session not found: {session_id}")
        return self.sessions[session_id]["page"]

    async def close_session(self, session_id: str) -> bool:
        if session_id in self.sessions:
            await self.sessions.pop(session_id)["context"].close()
            return True
        return False


browser_manager = BrowserSessionManager()


async def browser_open_url(url: str, event_bus: EventBus, _context: Optional[Dict] = None) -> Dict:
    _check_url_policy(url)
    session_id = None
    try:
        session_id = await browser_manager.create_session()
        await event_bus.append(make_event("browser.session.created", {"session_id": session_id}, context=_context or {}))
        page = await browser_manager.get_page(session_id)
        await page.goto(url, timeout=BROWSER_ACTION_TIMEOUT * 1000, wait_until="domcontentloaded")
        title = await page.title()
        await event_bus.append(make_event("browser.navigate", {"session_id": session_id, "url": url, "title": title}, context=_context or {}))
        return {"session_id": session_id, "url": url, "title": title, "status": "opened"}
    except Exception as e:
        await event_bus.append(make_event("browser.error", {"session_id": session_id, "action": "open_url", "url": url, "error": str(e)}, context=_context or {}))
        if session_id:
            await browser_manager.close_session(session_id)
        raise


async def browser_create_session(event_bus: EventBus, _context: Optional[Dict] = None) -> Dict:
    sid = await browser_manager.create_session()
    await event_bus.append(make_event("browser.session.created", {"session_id": sid}, context=_context or {}))
    return {"session_id": sid}


async def browser_navigate(session_id: str, url: str, event_bus: EventBus, _context: Optional[Dict] = None) -> Dict:
    _check_url_policy(url)
    try:
        page = await browser_manager.get_page(session_id)
        await page.goto(url, timeout=BROWSER_ACTION_TIMEOUT * 1000, wait_until="domcontentloaded")
        title = await page.title()
        await event_bus.append(make_event("browser.navigate", {"session_id": session_id, "url": url, "title": title}, context=_context or {}))
        return {"status": "navigated", "url": url, "title": title}
    except Exception as e:
        await event_bus.append(make_event("browser.error", {"session_id": session_id, "action": "navigate", "error": str(e)}, context=_context or {}))
        raise


async def browser_click(session_id: str, selector: str, event_bus: EventBus, _context: Optional[Dict] = None) -> Dict:
    try:
        page = await browser_manager.get_page(session_id)
        await page.click(selector, timeout=BROWSER_ACTION_TIMEOUT * 1000)
        await event_bus.append(make_event("browser.click", {"session_id": session_id, "selector": selector}, context=_context or {}))
        return {"status": "clicked", "selector": selector}
    except Exception as e:
        await event_bus.append(make_event("browser.error", {"session_id": session_id, "action": "click", "error": str(e)}, context=_context or {}))
        raise


async def browser_type(session_id: str, selector: str, text: str, event_bus: EventBus, _context: Optional[Dict] = None) -> Dict:
    try:
        page = await browser_manager.get_page(session_id)
        await page.fill(selector, text, timeout=BROWSER_ACTION_TIMEOUT * 1000)
        await event_bus.append(make_event("browser.type", {"session_id": session_id, "selector": selector, "text": text[:50]}, context=_context or {}))
        return {"status": "typed", "selector": selector}
    except Exception as e:
        await event_bus.append(make_event("browser.error", {"session_id": session_id, "action": "type", "error": str(e)}, context=_context or {}))
        raise


async def browser_screenshot(session_id: str, path: str = "screenshot.png", event_bus: EventBus = None, _context: Optional[Dict] = None) -> Dict:
    try:
        page = await browser_manager.get_page(session_id)
        safe_path = resolve_workspace_path(path)
        await page.screenshot(path=str(safe_path), full_page=True)
        rel_path = str(safe_path.relative_to(WORKSPACE_ROOT))
        if event_bus:
            await event_bus.append(make_event("browser.screenshot", {"session_id": session_id, "path": rel_path}, context=_context or {}))
        return {"screenshot_path": rel_path, "full_page": True}
    except Exception as e:
        if event_bus:
            await event_bus.append(make_event("browser.error", {"session_id": session_id, "action": "screenshot", "error": str(e)}, context=_context or {}))
        raise


async def browser_dom_snapshot(
    session_id: str,
    include_html: bool = False,
    max_text_chars: int = 8000,
    event_bus: EventBus = None,
    _context: Optional[Dict] = None,
) -> Dict:
    try:
        page = await browser_manager.get_page(session_id)
        url = page.url
        title = await page.title()

        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        text = (text or "")[:max_text_chars]

        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).slice(0, 50).map(a => ({
                text: a.innerText.trim().slice(0, 80),
                href: a.href,
                selector: a.id ? '#' + CSS.escape(a.id) :
                          a.getAttribute('data-testid') ? '[data-testid="' + a.getAttribute('data-testid') + '"]' :
                          'a'
            })).filter(l => l.href && !l.href.startsWith('javascript:'))
        """)

        headings = await page.evaluate("""
            () => Array.from(document.querySelectorAll('h1,h2,h3')).slice(0, 20)
                .map(h => ({level: h.tagName, text: h.innerText.trim().slice(0, 100)}))
        """)

        # Clickables with CSS.escape for safe selectors
        clickables = await page.evaluate("""
            () => Array.from(document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], [role="button"], a[href]'
            )).slice(0, 30).map(el => {
                let selector = '';
                if (el.id) selector = '#' + CSS.escape(el.id);
                else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';
                else if (el.name) selector = el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
                else if (el.getAttribute('aria-label')) selector = '[aria-label="' + el.getAttribute('aria-label') + '"]';
                else selector = el.tagName.toLowerCase();
                return {
                    text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 80),
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    role: el.getAttribute('role') || '',
                    selector: selector
                };
            }).filter(e => e.selector)
        """)

        # Inputs with CSS.escape
        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), textarea, select'
            )).slice(0, 20).map(el => {
                let selector = '';
                if (el.id) selector = '#' + CSS.escape(el.id);
                else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';
                else if (el.name) selector = el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
                else selector = el.tagName.toLowerCase() + '[type="' + (el.type || 'text') + '"]';
                return {
                    tag: el.tagName.toLowerCase(), type: el.type || '',
                    name: el.name || '', id: el.id || '',
                    placeholder: el.placeholder || '', selector: selector
                };
            })
        """)

        result = {
            "session_id": session_id, "url": url, "title": title,
            "text": text, "headings": headings, "links": links,
            "clickables": clickables, "inputs": inputs,
        }
        if include_html:
            result["html"] = (await page.content())[:50000]

        if event_bus:
            await event_bus.append(make_event(
                "browser.dom_snapshot",
                {"session_id": session_id, "url": url, "title": title,
                 "text_chars": len(text), "links_count": len(links),
                 "clickables_count": len(clickables), "inputs_count": len(inputs)},
                context=_context or {},
            ))
        return result
    except Exception as e:
        if event_bus:
            await event_bus.append(make_event("browser.error", {"session_id": session_id, "action": "dom_snapshot", "error": str(e)}, context=_context or {}))
        raise


async def browser_close(session_id: str, event_bus: EventBus, _context: Optional[Dict] = None) -> Dict:
    closed = await browser_manager.close_session(session_id)
    if closed:
        await event_bus.append(make_event("browser.close", {"session_id": session_id}, context=_context or {}))
        return {"closed": True}
    else:
        # Emit browser.error THEN raise — so both event log and tool status are correct
        err_msg = f"Browser session not found: {session_id}"
        await event_bus.append(make_event(
            "browser.error",
            {"session_id": session_id, "action": "close", "error": err_msg},
            context=_context or {},
        ))
        raise ValueError(err_msg)
