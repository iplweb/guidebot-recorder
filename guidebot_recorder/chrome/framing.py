"""Strip framing-protection headers so arbitrary sites load inside an iframe.

The render step loads target sites inside a shell iframe. Sites commonly defend
against being framed with the ``X-Frame-Options`` header and the CSP
``frame-ancestors`` directive; both must be neutralised on the top-level
document response.

Redirects: Chromium rejects a route-*fulfilled* 3xx response inside a subframe
(``net::ERR_BLOCKED_BY_RESPONSE``), so passing a redirect through cannot be
combined with header stripping in an iframe. We therefore let ``route.fetch``
follow redirects internally and fulfill the *final* response with its framing
headers stripped. Consequence: a site that redirects on its entry URL still
loads, but the frame commits at the entry URL — ``frame.url`` (and the address
pill) shows the navigated URL, not the post-redirect one. Sub-resources with
*relative* URLs on a cross-origin-redirecting page resolve against the entry
URL; sites using absolute URLs (the common case) are unaffected.
"""

from __future__ import annotations

import re

from playwright.async_api import BrowserContext, Route

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

    async def handler(route: Route) -> None:
        try:
            resource_type = route.request.resource_type
            if resource_type in ("document", "subframe"):
                # Default max_redirects follows the chain and returns the final
                # response; a fulfilled 2xx is accepted in a subframe where a
                # fulfilled 3xx is not.
                resp = await route.fetch()
                await route.fulfill(
                    response=resp,
                    headers=strip_framing_headers(dict(resp.headers), is_document=True),
                )
            else:
                await route.continue_()
        except Exception:
            await route.continue_()

    await context.route("**/*", handler)
