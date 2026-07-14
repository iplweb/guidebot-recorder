"""Build trusted Playwright locators and validate resolved actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from guidebot_recorder.models.action import ActionKind, CachedAction
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    Target,
    TestidTarget,
    TextTarget,
)
from guidebot_recorder.resolver.identity_capture import capture_identity


ValidationReason: TypeAlias = Literal[
    "not_found",
    "not_unique",
    "not_visible",
    "not_enabled",
    "not_editable",
    "incompatible_type",
    "unsupported_action",
]


@dataclass(frozen=True, slots=True)
class ValidationOk:
    """A locator that passed all compile-time checks."""

    locator: Locator


@dataclass(frozen=True, slots=True)
class ValidationFail:
    """A stable failure reason suitable for a resolver re-prompt."""

    reason: ValidationReason
    message: str


LocatorRoot: TypeAlias = Page | Locator


def _build_locator(root: LocatorRoot, target: Target) -> Locator:
    if target.scope is not None:
        root = _build_locator(root, target.scope)

    if isinstance(target, RoleTarget):
        locator = root.get_by_role(
            target.role,  # type: ignore[arg-type]
            name=target.name,
            exact=target.exact,
        )
        if target.nth is not None:
            locator = locator.nth(target.nth)
        return locator
    if isinstance(target, TextTarget):
        return root.get_by_text(target.text, exact=target.exact)
    if isinstance(target, LabelTarget):
        return root.get_by_label(target.label, exact=target.exact)
    if isinstance(target, TestidTarget):
        return root.get_by_test_id(target.testid)

    # ``Target`` is exhaustive, but this also makes hand-built foreign objects fail
    # closed instead of ever becoming an arbitrary selector.
    raise TypeError(f"Unsupported target type: {type(target).__name__}")


async def build_locator(page: Page, target: Target) -> Locator:
    """Build a locator exclusively from the structural ``Target`` fields."""

    return _build_locator(page, target)


async def _is_type_compatible(locator: Locator) -> bool:
    """Return whether Playwright can safely enter text into the matched element."""

    return await locator.evaluate(
        """
        element => {
          const tag = element.tagName.toLowerCase();
          if (tag === "textarea") return true;
          if (tag === "input") {
            const nonTextTypes = new Set([
              "button", "checkbox", "color", "file", "hidden", "image",
              "radio", "range", "reset", "submit"
            ]);
            return !nonTextTypes.has((element.getAttribute("type") || "text").toLowerCase());
          }
          return element.isContentEditable;
        }
        """
    )


async def validate_compile_time(
    page: Page, target: Target, action: ActionKind
) -> ValidationOk | ValidationFail:
    """Apply the compile-time half of the resolver's trust-but-verify contract."""

    if action not in ("click", "hover", "type", "waitFor"):
        return ValidationFail(
            "unsupported_action", f"Unsupported action kind: {action!r}."
        )

    locator = await build_locator(page, target)
    count = await locator.count()
    if count == 0:
        return ValidationFail("not_found", "The target locator matched no elements.")
    if count != 1:
        return ValidationFail(
            "not_unique", f"The target locator matched {count} elements; expected 1."
        )

    if not await locator.is_visible():
        return ValidationFail("not_visible", "The matched element is not visible.")

    if action == "type" and not await _is_type_compatible(locator):
        return ValidationFail(
            "incompatible_type", "The type action requires a text-entry element."
        )

    if action in ("click", "type") and not await locator.is_enabled():
        return ValidationFail("not_enabled", "The matched element is disabled.")

    if action == "type" and not await locator.is_editable():
        return ValidationFail("not_editable", "The matched element is not editable.")

    return ValidationOk(locator)


async def reuse_is_valid(page: Page, cached: CachedAction) -> bool:
    """Validate a cached target and its independent, frozen identity."""

    result = await validate_compile_time(page, cached.target, cached.action)
    if isinstance(result, ValidationFail) or cached.identity is None:
        return False

    try:
        current_identity = await capture_identity(result.locator)
    except (PlaywrightError, ValueError):
        # The DOM may change between the locator checks and identity capture.
        return False
    return cached.identity.matches(current_identity)

