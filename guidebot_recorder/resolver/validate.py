"""Build trusted Playwright locators and validate resolved actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

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
    "dom_changed",
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


LocatorRoot: TypeAlias = Page | Frame | Locator

_SENSITIVE_AUTOCOMPLETE = {
    "current-password",
    "new-password",
    "one-time-code",
    "cc-number",
    "cc-csc",
}
_SENSITIVE_FIELD_METADATA = re.compile(
    r"\b(password|passwd|passcode|passphrase|credential\w*|secret|token|"
    r"otp|pin|cvv|cvc|ssn|pesel|security[\s_-]*code|verification[\s_-]*code|"
    r"recovery[\s_-]*code|access[\s_-]*code|auth(?:entication)?[\s_-]*code|"
    r"hasł\w*|sekret\w*|kod\w*\s+(?:bezpieczeństwa|dostępu|weryfikacyjn\w*|"
    r"uwierzytelniając\w*)|numer\w*\s+karty)\b",
    re.IGNORECASE,
)


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


async def build_locator(page: Page | Frame, target: Target) -> Locator:
    """Build a locator exclusively from the structural ``Target`` fields.

    Accepts a ``Page`` or a ``Frame`` (the main window resolves against the shell
    site iframe); both expose the same ``get_by_*`` locator factory.
    """

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


async def is_sensitive_type_target(locator: Locator) -> bool:
    """Fail closed for fields where a frozen ``teach`` literal may expose a secret."""

    metadata = await locator.evaluate(
        """
        element => ({
          type: (element.getAttribute("type") || "").toLowerCase(),
          autocomplete: (element.getAttribute("autocomplete") || "").toLowerCase(),
          text: [
            element.getAttribute("aria-label"),
            element.getAttribute("placeholder"),
            element.getAttribute("name"),
            element.getAttribute("id"),
            ...Array.from(element.labels || [], label => label.textContent),
          ].filter(Boolean).join(" "),
        })
        """
    )
    if not isinstance(metadata, dict):
        return True
    if metadata.get("type") == "password":
        return True
    autocomplete = metadata.get("autocomplete")
    if not isinstance(autocomplete, str):
        return True
    if _SENSITIVE_AUTOCOMPLETE.intersection(autocomplete.split()):
        return True
    text = metadata.get("text")
    return not isinstance(text, str) or _SENSITIVE_FIELD_METADATA.search(text) is not None


async def validate_compile_time(
    page: Page | Frame, target: Target, action: ActionKind
) -> ValidationOk | ValidationFail:
    """Apply the compile-time half of the resolver's trust-but-verify contract."""

    if action not in ("click", "hover", "type", "waitFor"):
        return ValidationFail("unsupported_action", f"Unsupported action kind: {action!r}.")

    try:
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

        # Close the largest count/check race window. The DOM can always mutate
        # after this function returns, so execution performs its own checks too.
        if await locator.count() != 1:
            return ValidationFail("dom_changed", "The target changed while it was being validated.")
        return ValidationOk(locator)
    except PlaywrightError:
        return ValidationFail("dom_changed", "The target changed while it was being validated.")


async def reuse_is_valid(page: Page | Frame, cached: CachedAction) -> bool:
    """Validate a cached target and its independent, frozen identity."""

    try:
        if cached.action == "waitFor":
            if cached.state is None:
                return False
            if cached.state == "hidden":
                # A hidden wait intentionally has no identity at the point it
                # succeeds. Zero matches already satisfies the condition; one
                # match is safe to wait on; multiple matches are ambiguous.
                locator = await build_locator(page, cached.target)
                return await locator.count() <= 1

        result = await validate_compile_time(page, cached.target, cached.action)
        if isinstance(result, ValidationFail) or cached.identity is None:
            return False
        if (
            cached.action == "type"
            and cached.fingerprint.command_kind == "teach"
            and await is_sensitive_type_target(result.locator)
        ):
            return False
        current_identity = await capture_identity(result.locator)
    except (PlaywrightError, ValueError):
        # The DOM may change between the locator checks and identity capture.
        return False
    return cached.identity.matches(current_identity)
