"""Tests for capturing a locator's frozen structural identity (Task 10)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.models.identity import Identity
from guidebot_recorder.resolver.identity_capture import capture_identity


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def test_capture_identity_normalizes_element_fields(page: Page) -> None:
    await page.set_content(
        """
        <base href="https://example.test/app/">
        <main role="main">
          <nav role="navigation">
            <A href="/folder/../x" data-testid="lnk">Open</A>
          </nav>
        </main>
        """
    )

    identity = await capture_identity(page.get_by_test_id("lnk"))

    assert isinstance(identity, Identity)
    assert identity.tag == "a"
    assert identity.testid == "lnk"
    assert identity.href == "https://example.test/x"
    assert identity.identity_version == 1


async def test_ancestry_digest_is_stable_sha256_of_tag_role_pairs(
    page: Page,
) -> None:
    await page.set_content(
        """
        <main role="main">
          <section id="first-parent" role="region" data-noise="one">
            <a id="first">First</a>
          </section>
          <section id="second-parent" role="region" data-noise="two">
            <a id="second">Second</a>
          </section>
          <div id="different-tag-parent" role="region">
            <a id="different-tag">Different tag</a>
          </div>
          <section id="different-role-parent" role="group">
            <a id="different-role">Different role</a>
          </section>
        </main>
        """
    )

    first = await capture_identity(page.locator("#first"))
    repeated = await capture_identity(page.locator("#first"))
    same_pairs = await capture_identity(page.locator("#second"))
    different_tag = await capture_identity(page.locator("#different-tag"))
    different_role = await capture_identity(page.locator("#different-role"))

    assert re.fullmatch(r"[0-9a-f]{64}", first.ancestry_digest)
    assert repeated.ancestry_digest == first.ancestry_digest
    assert same_pairs.ancestry_digest == first.ancestry_digest
    assert different_tag.ancestry_digest != first.ancestry_digest
    assert different_role.ancestry_digest != first.ancestry_digest
