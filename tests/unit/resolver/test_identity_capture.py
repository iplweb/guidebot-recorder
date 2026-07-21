"""Tests for capturing a locator's frozen structural identity (Task 10)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.models.identity import Identity
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.page_context import candidate_ids_of, collect_candidates


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
    assert identity.dom_path_digest is not None


async def test_dom_path_digest_is_the_candidate_id_of_the_same_element(page: Page) -> None:
    """Cross-comparisons between the two only work if both count the same hash."""

    await page.set_content(
        """
        <button>Pierwszy</button>
        <button>Drugi</button>
        """
    )

    identity = await capture_identity(page.get_by_role("button").nth(1))
    candidates = {candidate.name: candidate for candidate in await collect_candidates(page)}

    assert identity.dom_path_digest == candidates["Drugi"].id
    assert identity.dom_path_digest == (await candidate_ids_of(page.get_by_role("button")))[1]


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


async def test_ancestry_uses_effective_implicit_and_fallback_roles(
    page: Page,
) -> None:
    await page.set_content(
        """
        <main role="main">
          <nav>
            <button id="implicit-role">Implicit role</button>
          </nav>
          <nav role="navigation">
            <button id="redundant-explicit-role">Explicit role</button>
          </nav>
          <nav role="not-a-real-role navigation">
            <button id="fallback-role">Fallback role</button>
          </nav>
        </main>
        """
    )

    implicit = await capture_identity(page.locator("#implicit-role"))
    explicit = await capture_identity(page.locator("#redundant-explicit-role"))
    fallback = await capture_identity(page.locator("#fallback-role"))

    assert implicit.ancestry_digest == explicit.ancestry_digest
    assert fallback.ancestry_digest == explicit.ancestry_digest


async def test_ancestry_crosses_open_shadow_host_to_composed_root(
    page: Page,
) -> None:
    await page.set_content(
        """
        <main role="main">
          <section role="region"><div id="first-host"></div></section>
          <aside role="complementary"><div id="second-host"></div></aside>
        </main>
        <script>
          document.querySelector("#first-host")
            .attachShadow({mode: "open"})
            .innerHTML = '<div role="group"><button id="first-shadow-target">First</button></div>';
          document.querySelector("#second-host")
            .attachShadow({mode: "open"})
            .innerHTML = '<div role="group"><button id="second-shadow-target">Second</button></div>';
        </script>
        """
    )

    first = await capture_identity(page.locator("#first-shadow-target"))
    second = await capture_identity(page.locator("#second-shadow-target"))

    assert first.ancestry_digest != second.ancestry_digest


async def test_capture_identity_normalizes_area_href(page: Page) -> None:
    await page.set_content(
        """
        <base href="https://example.test/maps/nested/">
        <map name="destinations">
          <area
            data-testid="target-area"
            href="../../docs/../target?mode=full#details"
            shape="rect"
            coords="0,0,10,10"
          >
        </map>
        """
    )

    identity = await capture_identity(page.get_by_test_id("target-area"))

    assert identity.tag == "area"
    assert identity.href == "https://example.test/target?mode=full#details"
