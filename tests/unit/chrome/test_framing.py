from __future__ import annotations

from playwright.async_api import Error as PlaywrightError

from guidebot_recorder.chrome.framing import install_framing, strip_framing_headers


class TestStripFramingHeaders:
    def test_removes_x_frame_options_lowercase(self) -> None:
        headers = {"x-frame-options": "DENY", "content-type": "text/html"}
        result = strip_framing_headers(headers, is_document=True)
        assert "x-frame-options" not in {k.lower() for k in result}
        assert result["content-type"] == "text/html"

    def test_removes_x_frame_options_titlecase(self) -> None:
        headers = {"X-Frame-Options": "SAMEORIGIN"}
        result = strip_framing_headers(headers, is_document=True)
        assert result == {}

    def test_strips_frame_ancestors_but_keeps_other_csp_directives(self) -> None:
        headers = {
            "Content-Security-Policy": (
                "default-src 'self'; frame-ancestors 'none'; script-src 'self'"
            )
        }
        result = strip_framing_headers(headers, is_document=True)
        csp = result["Content-Security-Policy"]
        assert "frame-ancestors" not in csp
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp

    def test_strips_frame_ancestors_at_end_of_csp(self) -> None:
        headers = {"content-security-policy": "script-src 'self'; frame-ancestors 'none'"}
        result = strip_framing_headers(headers, is_document=True)
        csp = result["content-security-policy"]
        assert "frame-ancestors" not in csp
        assert "script-src 'self'" in csp

    def test_csp_with_only_frame_ancestors_is_dropped(self) -> None:
        headers = {"Content-Security-Policy": "frame-ancestors 'none'"}
        result = strip_framing_headers(headers, is_document=True)
        assert "content-security-policy" not in {k.lower() for k in result}

    def test_csp_with_only_frame_ancestors_and_trailing_semicolon_is_dropped(self) -> None:
        headers = {"Content-Security-Policy": "frame-ancestors 'self';"}
        result = strip_framing_headers(headers, is_document=True)
        assert "content-security-policy" not in {k.lower() for k in result}

    def test_non_document_returns_unchanged(self) -> None:
        headers = {
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'; default-src 'self'",
        }
        result = strip_framing_headers(headers, is_document=False)
        assert result == headers

    def test_input_dict_not_mutated(self) -> None:
        headers = {
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'; default-src 'self'",
        }
        original = dict(headers)
        strip_framing_headers(headers, is_document=True)
        assert headers == original

    def test_unrelated_headers_preserved_with_casing(self) -> None:
        headers = {
            "Content-Type": "text/html",
            "X-Custom-Header": "value",
            "X-Frame-Options": "DENY",
        }
        result = strip_framing_headers(headers, is_document=True)
        assert result["Content-Type"] == "text/html"
        assert result["X-Custom-Header"] == "value"

    def test_no_framing_headers_returns_equal_copy(self) -> None:
        headers = {"Content-Type": "text/html", "Cache-Control": "no-cache"}
        result = strip_framing_headers(headers, is_document=True)
        assert result == headers
        assert result is not headers

    def test_case_insensitive_frame_ancestors_directive(self) -> None:
        headers = {"Content-Security-Policy": "default-src 'self'; FRAME-ANCESTORS 'none'"}
        result = strip_framing_headers(headers, is_document=True)
        assert "frame-ancestors" not in result["Content-Security-Policy"].lower()
        assert "default-src 'self'" in result["Content-Security-Policy"]


class _FakeFrame:
    def __init__(self, parent_frame: _FakeFrame | None = None) -> None:
        self.parent_frame = parent_frame


class _FakeRequest:
    def __init__(
        self, resource_type: str, frame: _FakeFrame | None, frame_raises: bool
    ) -> None:
        self.resource_type = resource_type
        self._frame = frame
        self._frame_raises = frame_raises

    @property
    def frame(self) -> _FakeFrame | None:
        if self._frame_raises:
            # Playwright raises for a navigation issued before its frame exists.
            raise PlaywrightError("frame not available before creation")
        return self._frame


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers


#: A frame whose parent is another frame — i.e. the framed site (a sub-frame).
_SUBFRAME = _FakeFrame(parent_frame=_FakeFrame())
#: A top-level frame (no parent) — the main window / a settled popup.
_TOP_LEVEL = _FakeFrame(parent_frame=None)


class _FakeRoute:
    def __init__(
        self,
        resource_type: str,
        response: _FakeResponse | None = None,
        *,
        frame: _FakeFrame | None = _TOP_LEVEL,
        frame_raises: bool = False,
    ) -> None:
        self.request = _FakeRequest(resource_type, frame, frame_raises)
        self._response = response
        self.fulfilled: dict | None = None
        self.continued = False
        self.fetched_max_redirects: int | None = None

    async def fetch(self, *, max_redirects: int = 20) -> _FakeResponse:
        self.fetched_max_redirects = max_redirects
        assert self._response is not None
        return self._response

    async def fulfill(self, *, response=None, headers=None) -> None:
        self.fulfilled = {"response": response, "headers": headers}

    async def continue_(self) -> None:
        self.continued = True


class _FakeContext:
    def __init__(self) -> None:
        self.handler = None

    async def route(self, pattern, handler) -> None:
        self.pattern = pattern
        self.handler = handler


class TestInstallFraming:
    async def test_registers_route_handler(self) -> None:
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        assert context.pattern == "**/*"
        assert callable(context.handler)

    async def test_subframe_document_follows_chain_and_strips_final(self) -> None:
        # The framed site: a fulfilled 3xx is blocked in a sub-frame, so fetch
        # follows the chain (default max_redirects) and strips the final 2xx.
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        final = _FakeResponse(200, {"X-Frame-Options": "DENY", "Content-Type": "text/html"})
        route = _FakeRoute("document", final, frame=_SUBFRAME)
        await context.handler(route)
        assert route.fetched_max_redirects != 0  # followed the chain
        assert route.fulfilled is not None
        assert route.fulfilled["response"] is final
        assert "x-frame-options" not in {k.lower() for k in route.fulfilled["headers"]}

    async def test_top_level_document_2xx_is_stripped(self) -> None:
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        resp = _FakeResponse(200, {"X-Frame-Options": "DENY", "Content-Type": "text/html"})
        route = _FakeRoute("document", resp, frame=_TOP_LEVEL)
        await context.handler(route)
        assert route.fetched_max_redirects == 0  # top level does not follow the chain
        assert route.fulfilled is not None
        assert route.fulfilled["response"] is resp
        assert "x-frame-options" not in {k.lower() for k in route.fulfilled["headers"]}

    async def test_top_level_document_redirect_passes_through_unchanged(self) -> None:
        # Popups keep native redirects: a 3xx is fulfilled unchanged (no header
        # rewrite) so the browser performs it and page.url stays truthful.
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        redirect = _FakeResponse(301, {"location": "/final"})
        route = _FakeRoute("document", redirect, frame=_TOP_LEVEL)
        await context.handler(route)
        assert route.fetched_max_redirects == 0
        assert route.fulfilled == {"response": redirect, "headers": None}

    async def test_popup_navigation_before_frame_created_is_top_level(self) -> None:
        # A popup's first navigation is issued before its frame exists (frame
        # raises); it must be treated as top-level, so a 3xx passes through.
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        redirect = _FakeResponse(302, {"location": "/final"})
        route = _FakeRoute("document", redirect, frame_raises=True)
        await context.handler(route)
        assert route.fulfilled == {"response": redirect, "headers": None}

    async def test_non_document_resource_is_continued(self) -> None:
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        route = _FakeRoute("image")
        await context.handler(route)
        assert route.continued is True
        assert route.fulfilled is None

    async def test_handler_exception_falls_back_to_continue(self) -> None:
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")

        class _BoomRoute(_FakeRoute):
            async def fetch(self, *, max_redirects: int = 20):
                raise RuntimeError("boom")

        route = _BoomRoute("document", _FakeResponse(200, {}), frame=_TOP_LEVEL)
        await context.handler(route)
        assert route.continued is True
