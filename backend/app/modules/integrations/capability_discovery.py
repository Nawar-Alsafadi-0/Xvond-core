from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from backend.app.core.http_security import safe_http_request, validate_public_http_url
from backend.app.modules.integrations.openapi_contract import fetch_openapi_contract


MAX_SEARCH_RESULTS = 8
MAX_DOC_PAGES = 5
_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
_OPENAPI_HINTS = (
    "openapi",
    "swagger",
    "api-docs",
    "api_docs",
    "spec.json",
    "openapi.json",
    "swagger.json",
    "openapi.yaml",
    "openapi.yml",
)


def _clean_public_url(value: str) -> str | None:
    url = html.unescape(str(value or "").strip())
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        query = parse_qs(parsed.query)
        redirect = (query.get("uddg") or [None])[0]
        if redirect:
            url = unquote(redirect)
    try:
        return validate_public_http_url(url)
    except Exception:
        return None


def _search_result_urls(query: str) -> list[str]:
    q = str(query or "").strip()[:240]
    if not q:
        return []
    result = safe_http_request(
        url=_SEARCH_URL.format(query=quote_plus(q)),
        method="GET",
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Xvond-Capability-Discovery/1.0",
        },
        timeout=15,
        max_response_bytes=500_000,
    )
    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        return []
    soup = BeautifulSoup(str(result.get("response") or ""), "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a.result__a, a.result-link, a[href]"):
        url = _clean_public_url(anchor.get("href"))
        if not url or url in urls:
            continue
        urls.append(url)
        if len(urls) >= MAX_SEARCH_RESULTS:
            break
    return urls


def _openapi_links_from_page(url: str) -> list[str]:
    result = safe_http_request(
        url=url,
        method="GET",
        headers={
            "Accept": "text/html,text/plain,application/json,application/yaml,*/*;q=0.1",
            "User-Agent": "Xvond-Capability-Discovery/1.0",
        },
        timeout=15,
        max_response_bytes=500_000,
    )
    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        return []
    body = str(result.get("response") or "")
    lowered = body.lower()
    candidates: list[str] = []

    if any(token in url.lower() for token in _OPENAPI_HINTS):
        candidates.append(url)

    stripped = body.lstrip()
    if stripped.startswith("{") and (
        '"openapi"' in lowered[:5000] or '"swagger"' in lowered[:5000]
    ):
        candidates.append(url)

    soup = BeautifulSoup(body, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        label = str(anchor.get_text(" ", strip=True) or "").lower()
        combined = (href + " " + label).lower()
        if not any(token in combined for token in _OPENAPI_HINTS):
            continue
        candidate = _clean_public_url(urljoin(url, href))
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        if len(candidates) >= 8:
            break
    return candidates


def discover_openapi_contract(discovery: dict) -> dict:
    """Resolve a compiler discovery plan into a verified public OpenAPI contract.

    Discovery only fetches public documentation. It never executes instructions
    found in pages, sends credentials, or calls write endpoints.
    """

    if not isinstance(discovery, dict) or discovery.get("needed") is not True:
        raise ValueError("Capability discovery plan is missing")

    explicit = str(discovery.get("docs_url") or "").strip()
    attempted: list[str] = []
    if explicit:
        attempted.append(explicit)
        try:
            contract = fetch_openapi_contract(explicit)
            return {
                "status": "resolved",
                "source": "grounded_docs_url",
                "docs_url": explicit,
                "contract": contract,
                "attempted": attempted,
            }
        except Exception:
            pass

    queries = [
        str(item or "").strip()[:240]
        for item in (discovery.get("search_queries") or [])
        if str(item or "").strip()
    ][:5]
    service_hint = str(discovery.get("service_hint") or "").strip()
    capability = str(discovery.get("capability") or "").strip()
    if not queries:
        seed = " ".join(part for part in (service_hint, capability, "OpenAPI Swagger API docs") if part)
        if seed:
            queries = [seed[:240]]

    page_urls: list[str] = []
    for query in queries:
        for url in _search_result_urls(query):
            if url not in page_urls:
                page_urls.append(url)
            if len(page_urls) >= MAX_DOC_PAGES:
                break
        if len(page_urls) >= MAX_DOC_PAGES:
            break

    candidate_specs: list[str] = []
    for page_url in page_urls:
        attempted.append(page_url)
        try:
            links = _openapi_links_from_page(page_url)
        except Exception:
            links = []
        for link in links:
            if link not in candidate_specs:
                candidate_specs.append(link)

    for candidate in candidate_specs[:8]:
        if candidate not in attempted:
            attempted.append(candidate)
        try:
            contract = fetch_openapi_contract(candidate)
        except Exception:
            continue
        return {
            "status": "resolved",
            "source": "public_docs_search",
            "docs_url": candidate,
            "contract": contract,
            "attempted": attempted[:20],
        }

    return {
        "status": "not_found",
        "source": "public_docs_search",
        "docs_url": "",
        "contract": None,
        "attempted": attempted[:20],
    }


def public_api_probe(contract: dict) -> dict | None:
    """Return validation evidence for a discovered unauthenticated API.

    Only a GET operation with no path variables and no required query
    parameters may be used. Discovery never probes write endpoints.
    """
    if not isinstance(contract, dict):
        return None
    base_url = str(contract.get("base_url") or "").strip().rstrip("/")
    operations = contract.get("operations")
    if not base_url or not isinstance(operations, dict):
        return None

    for name, operation in operations.items():
        if not isinstance(operation, dict):
            continue
        if str(operation.get("method") or "").upper() != "GET":
            continue
        if operation.get("path_params"):
            continue
        if operation.get("required_query_params"):
            continue
        endpoint = str(operation.get("endpoint") or "").strip()
        if not endpoint or endpoint.startswith("//") or endpoint.lower().startswith(("http://", "https://")):
            continue
        url = base_url + "/" + endpoint.lstrip("/")
        try:
            result = safe_http_request(
                url=url,
                method="GET",
                headers={
                    "Accept": "application/json,text/plain,*/*;q=0.1",
                    "User-Agent": "Xvond-Capability-Discovery/1.0",
                },
                timeout=10,
                max_response_bytes=64_000,
            )
        except Exception:
            continue
        status = int(result.get("status_code") or 0)
        if 200 <= status < 300:
            return {
                "validated": True,
                "validated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "mode": "discovered_public_api_safe_get",
                "operation": str(name),
                "endpoint": "/" + endpoint.lstrip("/"),
                "status_code": status,
            }
    return None
