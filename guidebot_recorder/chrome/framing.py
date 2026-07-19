"""Strip framing-protection headers so arbitrary sites load inside an iframe.

The render step loads target sites inside a shell iframe. Sites commonly defend
against being framed with the ``X-Frame-Options`` header and the CSP
``frame-ancestors`` directive; both must be neutralised on the top-level
document response.

Redirects differ by frame level (Playwright reports both the framed site's
navigation and a popup's navigation as ``resource_type == "document"``, so the
resource type cannot tell them apart — the request's frame hierarchy can):

- **Framed site** (a request inside an existing sub-frame): Chromium rejects a
  route-*fulfilled* 3xx inside a sub-frame (``net::ERR_BLOCKED_BY_RESPONSE``), so
  a redirect cannot be passed through and header-stripped there. We let
  ``route.fetch`` follow the chain internally and fulfill the *final* response
  with framing headers stripped. Consequence: the site still loads, but the
  frame commits at the entry URL — ``frame.url`` (and the pill) shows the
  navigated URL, not the post-redirect one, and *relative* sub-resources resolve
  against the entry URL (absolute URLs, the common case, are unaffected).
- **Top-level document** (the main window aside — in practice popups): a
  fulfilled 3xx is legal at the top level, so we pass redirects through
  unchanged and only strip headers off a 2xx. This keeps popups behaving as
  before Spec A — the browser performs redirects natively and ``page.url`` stays
  truthful.
"""

from __future__ import annotations

import re

from playwright.async_api import BrowserContext, Route
from playwright.async_api import Error as PlaywrightError

_CSP_HEADER = "content-security-policy"
_XFO_HEADER = "x-frame-options"

# Matches a ``frame-ancestors`` directive up to (and including) the next
# semicolon or end of string. Case-insensitive; tolerates leading whitespace.
_FRAME_ANCESTORS_RE = re.compile(r"\s*frame-ancestors[^;]*;?", re.IGNORECASE)


def _strip_frame_ancestors(csp: str) -> str:
    """Remove only the ``frame-ancestors`` directive from a CSP value."""
    cleaned = _FRAME_ANCESTORS_RE.sub("", csp)
    # Normalise separators left behind (e.g. ``; ;`` or a leading ``;``).
    directives = [part.strip() for part in cleaned.split(";")]
    return "; ".join(part for part in directives if part)


def strip_framing_headers(headers: dict[str, str], *, is_document: bool) -> dict[str, str]:
    """Return a copy of ``headers`` without framing-protection directives.

    Removes ``X-Frame-Options`` entirely and strips the ``frame-ancestors``
    directive from any ``Content-Security-Policy`` (dropping the CSP header if
    that leaves it empty). Non-document resources are returned unchanged. The
    input dict is never mutated and original header-name casing is preserved.
    """
    if not is_document:
        return dict(headers)

    result: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered == _XFO_HEADER:
            continue
        if lowered == _CSP_HEADER:
            cleaned = _strip_frame_ancestors(value)
            if not cleaned:
                continue
            result[name] = cleaned
            continue
        result[name] = value
    return result


async def install_framing(context: BrowserContext, *, shell_origin: str) -> None:
    """Route document responses through :func:`strip_framing_headers`.

    Registers a catch-all route on ``context``. Top-level document and subframe
    responses have their framing headers stripped; ``route.fetch`` follows any
    redirects internally and the final response is fulfilled with stripped
    headers (see module docstring for why a 3xx cannot be passed through in a
    subframe, and the entry-URL consequence). All other resource types pass
    through untouched. Any error falls back to :meth:`Route.continue_` rather
    than hanging the request.
    """

    def _in_subframe(route: Route) -> bool:
        """True when the request belongs to an existing sub-frame (the framed site).

        A top-level navigation is issued before its frame exists, so ``frame``
        raises — treat that (a popup) as top-level.
        """
        try:
            return route.request.frame.parent_frame is not None
        except PlaywrightError:
            return False

    async def handler(route: Route) -> None:
        try:
            if route.request.resource_type not in ("document", "subframe"):
                await route.continue_()
                return
            if _in_subframe(route):
                # Follow the chain and fulfill the final 2xx: a fulfilled 3xx is
                # blocked inside a sub-frame (see module docstring).
                resp = await route.fetch()
                await route.fulfill(
                    response=resp,
                    headers=strip_framing_headers(dict(resp.headers), is_document=True),
                )
            else:
                # Top-level (popups): pass 3xx through so the browser redirects
                # natively and page.url stays truthful; strip a 2xx.
                resp = await route.fetch(max_redirects=0)
                if 300 <= resp.status < 400:
                    await route.fulfill(response=resp)
                else:
                    await route.fulfill(
                        response=resp,
                        headers=strip_framing_headers(dict(resp.headers), is_document=True),
                    )
        except Exception:
            await route.continue_()

    await context.route("**/*", handler)
