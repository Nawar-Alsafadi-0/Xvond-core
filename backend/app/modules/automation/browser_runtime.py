from __future__ import annotations

from threading import BoundedSemaphore
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from backend.app.core.http_security import validate_public_http_url


class BrowserExecutionError(ValueError):
    pass


_MAX_ACTIONS = 30
_MAX_OUTPUT_CHARS = 50_000
_BROWSER_SLOTS = BoundedSemaphore(2)


def _validate_browser_url(url: str) -> str:
    clean = str(url or "").strip()
    if not clean:
        raise BrowserExecutionError("Browser URL is required")
    return validate_public_http_url(clean)


def _allowed_resource(url: str, cache: set[str]) -> bool:
    if url.startswith(("data:", "blob:")):
        return True
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in cache:
        return True
    try:
        validate_public_http_url(url)
    except Exception:
        return False
    cache.add(origin)
    return True


def _locator(page, action: dict):
    selector = str(action.get("selector") or "").strip()
    text = str(action.get("text") or "").strip()
    role = str(action.get("role") or "").strip()
    name = str(action.get("name") or "").strip()

    if selector:
        return page.locator(selector).first
    if role:
        kwargs = {"name": name} if name else {}
        return page.get_by_role(role, **kwargs).first
    if text:
        return page.get_by_text(text, exact=bool(action.get("exact", False))).first
    raise BrowserExecutionError("Browser action requires selector, role, or text locator")


def run_browser_task(
    *,
    start_url: str,
    actions: list[dict] | None = None,
    timeout_seconds: int = 30,
    allow_interactions: bool = False,
) -> dict:
    url = _validate_browser_url(start_url)
    steps = list(actions or [])
    if len(steps) > _MAX_ACTIONS:
        raise BrowserExecutionError(f"Browser task exceeds {_MAX_ACTIONS} actions")

    timeout_ms = max(5_000, min(int(timeout_seconds) * 1000, 60_000))
    if not _BROWSER_SLOTS.acquire(timeout=5):
        raise BrowserExecutionError("Browser capacity is temporarily unavailable")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                ],
            )
            context = browser.new_context(
                java_script_enabled=True,
                accept_downloads=False,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            allowed_origins: set[str] = set()

            def route_handler(route):
                request_url = route.request.url
                if _allowed_resource(request_url, allowed_origins):
                    route.continue_()
                else:
                    route.abort()

            page.route("**/*", route_handler)

            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserExecutionError("Browser navigation timed out") from exc

            outputs: list[dict] = []
            for index, raw in enumerate(steps):
                if not isinstance(raw, dict):
                    raise BrowserExecutionError(f"Browser action {index} must be an object")
                op = str(raw.get("op") or "").strip().lower()
                if op in {"click", "fill", "press", "select"} and not allow_interactions:
                    raise BrowserExecutionError(
                        f"Browser action {index} ({op}) requires explicit approval"
                    )

                try:
                    if op == "goto":
                        target = _validate_browser_url(str(raw.get("url") or ""))
                        page.goto(
                            target,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                        outputs.append({"index": index, "op": op, "url": page.url})

                    elif op == "click":
                        locator = _locator(page, raw)
                        locator.click(timeout=timeout_ms)
                        outputs.append({"index": index, "op": op, "clicked": True})

                    elif op == "fill":
                        value = raw.get("value")
                        if not isinstance(value, (str, int, float)):
                            raise BrowserExecutionError("Browser fill value must be scalar")
                        locator = _locator(page, raw)
                        locator.fill(str(value), timeout=timeout_ms)
                        outputs.append({"index": index, "op": op, "filled": True})

                    elif op == "press":
                        key = str(raw.get("key") or "").strip()
                        if not key or len(key) > 40:
                            raise BrowserExecutionError("Browser press requires a valid key")
                        locator = _locator(page, raw)
                        locator.press(key, timeout=timeout_ms)
                        outputs.append({"index": index, "op": op, "pressed": key})

                    elif op == "select":
                        value = raw.get("value")
                        if not isinstance(value, (str, int, float)):
                            raise BrowserExecutionError("Browser select value must be scalar")
                        locator = _locator(page, raw)
                        locator.select_option(str(value), timeout=timeout_ms)
                        outputs.append({"index": index, "op": op, "selected": str(value)})

                    elif op == "wait_for":
                        locator = _locator(page, raw)
                        locator.wait_for(
                            state=str(raw.get("state") or "visible"),
                            timeout=timeout_ms,
                        )
                        outputs.append({"index": index, "op": op, "ready": True})

                    elif op == "extract_text":
                        locator = _locator(page, raw)
                        value = locator.inner_text(timeout=timeout_ms)
                        outputs.append(
                            {
                                "index": index,
                                "op": op,
                                "value": str(value)[:_MAX_OUTPUT_CHARS],
                            }
                        )

                    elif op == "extract_attribute":
                        attribute = str(raw.get("attribute") or "").strip()
                        if not attribute or len(attribute) > 120:
                            raise BrowserExecutionError(
                                "Browser extract_attribute requires attribute"
                            )
                        locator = _locator(page, raw)
                        value = locator.get_attribute(attribute, timeout=timeout_ms)
                        outputs.append(
                            {
                                "index": index,
                                "op": op,
                                "value": None if value is None else str(value)[:_MAX_OUTPUT_CHARS],
                            }
                        )

                    elif op == "extract_html":
                        locator = _locator(page, raw)
                        value = locator.inner_html(timeout=timeout_ms)
                        outputs.append(
                            {
                                "index": index,
                                "op": op,
                                "value": str(value)[:_MAX_OUTPUT_CHARS],
                            }
                        )

                    else:
                        raise BrowserExecutionError(
                            f"Unsupported browser action: {op or 'missing'}"
                        )
                except PlaywrightTimeoutError as exc:
                    raise BrowserExecutionError(
                        f"Browser action {index} ({op}) timed out"
                    ) from exc

            result = {
                "url": page.url,
                "title": str(page.title())[:1000],
                "actions": outputs,
            }
            context.close()
            browser.close()
            return result
    finally:
        _BROWSER_SLOTS.release()
