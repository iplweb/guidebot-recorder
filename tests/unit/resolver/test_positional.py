"""`pin_position` / `pinned_drifted` on real Chromium — the machine counts nth.

The fixture reproduces the evidence of issue #51: a django-multiseek-style
criteria form with repeating rows. The trap the reviewer flagged is that
``<button>×</button>`` *has* the accessible name ``"×"`` (``accessibleName``
takes the ``textAlternative`` of a ``button``), so an unnamed delete button must
wrap its icon in an ``aria-hidden`` span. The genuinely unnamed controls in the
issue were textboxes (``role=textbox name=''``), so the "Zakres lat" (year range)
row carries two of them — the pair that got the *same* ``nth=1`` in the bug.
"""

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import RoleTarget, TextTarget
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.page_context import candidate_ids_of
from guidebot_recorder.resolver.positional import (
    PinFail,
    Pinned,
    pin_position,
    pinned_drifted,
)
from guidebot_recorder.resolver.validate import build_locator

#: Three rows with an unnamed delete button (icon hidden from the a11y tree),
#: a "Zakres lat" row with two unnamed textboxes, and one uniquely named button.
_FORM = """
<form>
  <div class="row"><span>Kryterium 1</span>
    <button class="del"><span aria-hidden="true">×</span></button></div>
  <div class="row"><span>Kryterium 2</span>
    <button class="del"><span aria-hidden="true">×</span></button></div>
  <div class="row"><span>Kryterium 3</span>
    <button class="del"><span aria-hidden="true">×</span></button></div>
  <div class="row"><span>Zakres lat</span>
    <input type="text" class="rok"><input type="text" class="rok"></div>
  <button>Szukaj</button>
</form>
"""

_UNNAMED_BUTTON = RoleTarget(role="button", name="", exact=True)
_UNNAMED_TEXTBOX = RoleTarget(role="textbox", name="", exact=True)


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


def _cached(target, identity: Identity | None, action: str = "click") -> CachedAction:
    return CachedAction(
        action=action,
        target=target,
        identity=identity,
        expect="none",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="usuń kryterium",
            expect="none",
            config_hash="config",
        ),
    )


async def _ids_of(page, target) -> list[str]:
    """The candidate ids the caller would hand `pin_position`, in `.nth(i)` order."""

    return await candidate_ids_of(await build_locator(page, target))


# --- pin_position: the spec rule table -------------------------------------


async def test_single_match_is_pinned_without_nth(page):
    await page.set_content(_FORM)

    result = await pin_position(page, RoleTarget(role="button", name="Szukaj"), None)

    assert isinstance(result, Pinned)
    assert result.matches == 1
    assert result.index is None
    assert isinstance(result.target, RoleTarget)
    assert result.target.nth is None


async def test_three_matches_and_the_third_id_pins_nth_2(page):
    await page.set_content(_FORM)
    ids = await _ids_of(page, _UNNAMED_BUTTON)
    assert len(ids) == 3

    result = await pin_position(page, _UNNAMED_BUTTON, ids[2])

    assert isinstance(result, Pinned)
    assert result.matches == 3
    assert result.index == 2
    assert result.target.nth == 2


async def test_no_candidate_id_with_many_matches_fails(page):
    await page.set_content(_FORM)

    result = await pin_position(page, _UNNAMED_BUTTON, None)

    assert isinstance(result, PinFail)
    assert result.reason == "no_candidate_id"


async def test_candidate_id_absent_from_matches_fails(page):
    await page.set_content(_FORM)

    result = await pin_position(page, _UNNAMED_BUTTON, "candidate-0000000000000000")

    assert isinstance(result, PinFail)
    assert result.reason == "candidate_not_matched"
    # message safety: only numbers and the candidate id token, no page text
    assert "candidate-0000000000000000" in result.message


async def test_zero_matches_is_not_found(page):
    await page.set_content(_FORM)

    result = await pin_position(page, RoleTarget(role="button", name="Nieistniejący"), None)

    assert isinstance(result, PinFail)
    assert result.reason == "not_found"


async def test_non_role_target_is_not_pinnable(page):
    await page.set_content(_FORM)

    result = await pin_position(page, TextTarget(text="Zakres lat"), None)

    assert isinstance(result, PinFail)
    assert result.reason == "not_pinnable"


async def test_two_year_steps_get_different_nth(page):
    """The most telling regression from the issue: two distinct steps that each
    named a different candidate must pin to *different* indices, not share one."""

    await page.set_content(_FORM)
    ids = await _ids_of(page, _UNNAMED_TEXTBOX)
    assert len(ids) == 2
    assert ids[0] != ids[1]

    od = await pin_position(page, _UNNAMED_TEXTBOX, ids[0])
    do = await pin_position(page, _UNNAMED_TEXTBOX, ids[1])

    assert isinstance(od, Pinned) and isinstance(do, Pinned)
    assert od.target.nth == 0
    assert do.target.nth == 1
    assert od.target.nth != do.target.nth


# --- pinned_drifted --------------------------------------------------------


async def test_drift_false_for_a_freshly_frozen_pin(page):
    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(1))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)

    assert await pinned_drifted(page, cached) is False


async def test_drift_true_after_a_row_is_injected_before_the_target(page):
    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(1))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)

    # Inject a fresh unnamed button ahead of the first row's button. This shifts
    # the target's positional DOM path (a uniform same-tag row wrapper would not:
    # it would move every row down one nth-of-type in lockstep, leaving the path
    # at the frozen index structurally identical — and correctly undetected).
    await page.evaluate(
        """() => {
          const firstRow = document.querySelector('.row');
          const button = document.createElement('button');
          const icon = document.createElement('span');
          icon.setAttribute('aria-hidden', 'true');
          icon.textContent = '×';
          button.appendChild(icon);
          firstRow.insertBefore(button, firstRow.firstChild);
        }"""
    )

    assert await pinned_drifted(page, cached) is True


async def test_drift_true_when_the_match_list_shrank_below_nth(page):
    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(2))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 2}), identity)

    await page.evaluate(
        """() => {
          const dels = Array.from(document.querySelectorAll('button.del'));
          dels.slice(1).forEach((button) => button.closest('.row').remove());
        }"""
    )

    assert await pinned_drifted(page, cached) is True


async def test_drift_false_when_identity_is_none(page):
    await page.set_content(_FORM)
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), None)

    assert await pinned_drifted(page, cached) is False


async def test_drift_false_when_dom_path_digest_is_none(page):
    await page.set_content(_FORM)
    identity = Identity(tag="button", ancestry_digest="whatever")  # dom_path_digest defaults None
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)

    assert cached.identity is not None and cached.identity.dom_path_digest is None
    assert await pinned_drifted(page, cached) is False
