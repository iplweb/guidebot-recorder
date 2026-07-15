from __future__ import annotations

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


class _FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers


class _FakeRoute:
    def __init__(self, resource_type: str, response: _FakeResponse | None = None) -> None:
        self.request = _FakeRequest(resource_type)
        self._response = response
        self.fulfilled: dict | None = None
        self.continued = False

    async def fetch(self, *, max_redirects: int = 20) -> _FakeResponse:
        # install_framing lets fetch follow redirects internally (no
        # max_redirects=0), so the final response is returned here.
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

    async def test_document_response_gets_headers_stripped(self) -> None:
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        resp = _FakeResponse(200, {"X-Frame-Options": "DENY", "Content-Type": "text/html"})
        route = _FakeRoute("document", resp)
        await context.handler(route)
        assert route.fulfilled is not None
        assert route.fulfilled["response"] is resp
        assert "x-frame-options" not in {k.lower() for k in route.fulfilled["headers"]}

    async def test_fetch_follows_redirects_and_final_is_stripped(self) -> None:
        # A route-fulfilled 3xx is blocked inside a subframe, so install_framing
        # lets fetch follow the chain (default max_redirects) and strips the
        # framing headers off the final response instead of passing 3xx through.
        context = _FakeContext()
        await install_framing(context, shell_origin="https://shell.example")
        final = _FakeResponse(200, {"X-Frame-Options": "DENY", "Content-Type": "text/html"})
        route = _FakeRoute("document", final)
        await context.handler(route)
        assert route.fetched_max_redirects != 0  # did not force max_redirects=0
        assert route.fulfilled is not None
        assert route.fulfilled["response"] is final
        assert "x-frame-options" not in {k.lower() for k in route.fulfilled["headers"]}

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

        route = _BoomRoute("document", _FakeResponse(200, {}))
        await context.handler(route)
        assert route.continued is True
