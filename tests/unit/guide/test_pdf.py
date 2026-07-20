from guidebot_recorder.guide.pdf import html_to_pdf


class _FakePage:
    def __init__(self):
        self.pdf_kwargs = None
        self.url = None

    async def goto(self, url, **_):
        self.url = url

    async def pdf(self, **kwargs):
        self.pdf_kwargs = kwargs

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self):
        self.page = _FakePage()

    async def new_page(self):
        return self.page


async def test_pdf_called_landscape_with_background(tmp_path):
    browser = _FakeBrowser()
    out = tmp_path / "g.pdf"
    await html_to_pdf(browser, "<html><body>hi</body></html>", out)
    assert browser.page.pdf_kwargs["landscape"] is True
    assert browser.page.pdf_kwargs["print_background"] is True
    assert browser.page.url.startswith("file://")
