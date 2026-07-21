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
from guidebot_recorder.resolver.widget import user_visible_control
from guidebot_recorder.selects.visibility import select_shape

ValidationReason: TypeAlias = Literal[
    "not_found",
    "not_unique",
    "not_visible",
    "not_enabled",
    "not_editable",
    "incompatible_type",
    "not_select",
    "option_missing",
    "unsupported_action",
    "dom_changed",
]

#: Reasons a *frozen, previously resolved* action can no longer be reused.
#: A superset of ``ValidationReason`` (the compile-time checks are reused as
#: the first stage of reuse validation) plus reasons specific to the frozen
#: identity/hidden-wait contract. Kept as a separate alias from
#: ``ValidationReason`` because that type also drives the resolver's
#: re-prompt vocabulary and must not grow reuse-only members.
ReuseReason: TypeAlias = (
    ValidationReason
    | Literal[
        "identity_mismatch",
        "identity_missing",
        "no_wait_state",
        "wait_ambiguous",
        "sensitive_target",
    ]
)

#: how many option labels an ``option_missing`` message spells out before eliding
_MAX_REPORTED_OPTIONS = 20


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


async def _is_native_select(locator: Locator) -> bool:
    """Return whether the matched element is a native ``<select>`` dropdown."""

    return await locator.evaluate("element => element.tagName.toLowerCase() === 'select'")


async def _is_page_enhanced(locator: Locator) -> bool:
    """Whether the page has taken this ``<select>`` over with a widget of its own.

    Decides whether the ``option_missing`` check below has anything to say about
    the element, and it has to be asked of all three control classes the select
    shim distinguishes:

    * a **shimmed** select and a **natively-visible listbox** (``multiple`` /
      ``size > 1``) are both driven straight off ``select.options`` — by the
      shim's ``optionIndexFor`` and by ``_OPTION_INDEX_JS`` respectively — so
      their option list *is* the thing execution will search, and an absent label
      is a real, checkable defect. Neither is "enhanced", so both get checked.
    * a select the page **enhanced itself** (select2, Tom Select, Chosen) is
      driven through the widget's own DOM list: beat 2 clicks the node that
      appeared after opening whose text equals the label, never an ``<option>``
      of the hidden original. Its ``options`` are therefore not evidence about
      the target at all, and for an AJAX-backed widget they are legitimately
      empty or partial until the user opens it. Checking them would reject a
      control this branch can genuinely drive, so it is not checked.

    The question is answered by the one shared predicate
    (:func:`guidebot_recorder.selects.select_shape`), never re-stated here — the
    same source ``selects.js`` classifies with and the recorder drives with.
    """

    return (await select_shape(locator))["enhanced"]


async def _select_option_labels(locator: Locator) -> list[str]:
    """The matched ``<select>``'s visible option labels, whitespace-normalised.

    Deliberately the same projection every execution path builds — ``option.label``
    falling back to the option's text, with runs of whitespace collapsed. That is
    ``optionLabel`` in ``selects.js`` (the shim), ``_OPTION_INDEX_JS`` in
    ``recorder.py`` (the natively-visible listbox) and Playwright's
    ``select_option(label=…)`` (compile's direct path and ``mode: native``), which
    are one rule by construction — so validation and execution read the same list.
    """

    return await locator.evaluate(
        """
        element => Array.from(
          element.options,
          option => (option.label || option.textContent || "").replace(/\\s+/g, " ").trim(),
        )
        """
    )


def _offers_option(labels: list[str], option: str) -> bool:
    """Whether ``option`` names one of ``labels`` under execution's matching rule.

    Whitespace is collapsed on both sides and the comparison is then **exact** —
    the single label→index rule of the select-shim design (§7), shared by
    ``optionIndexFor`` (the shim), ``_OPTION_INDEX_JS`` (the listbox path) and
    Playwright's ``select_option(label=…)``.

    Matching validation to that rule is the whole point. A looser comparison here
    would be the more dangerous mistake of the two: a label differing only in case
    would sail through validation, be frozen as the resolved target, and then fail
    during playback — which is exactly the late failure this check exists to
    eliminate. Stricter is not an option either, since it would send the resolver
    chasing a different element than the one it could have driven; the rules are
    the same rule, and ``test_validate_option_rule_matches_execution`` pins them
    to each other so they cannot drift apart again.
    """

    return " ".join(option.split()) in labels


def _option_missing_message(option: str, labels: list[str]) -> str:
    """Name both halves of the mismatch — a bare "no such option" re-prompts blind."""

    if not labels:
        return f"The <select> has no options at all, so {option!r} cannot be chosen."
    shown = ", ".join(repr(label) for label in labels[:_MAX_REPORTED_OPTIONS])
    if len(labels) > _MAX_REPORTED_OPTIONS:
        shown += f", … (+{len(labels) - _MAX_REPORTED_OPTIONS} more)"
    return f"The <select> has no option labelled {option!r}; it offers: {shown}."


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
    page: Page | Frame, target: Target, action: ActionKind, option: str | None = None
) -> ValidationOk | ValidationFail:
    """Apply the compile-time half of the resolver's trust-but-verify contract.

    ``option`` is the visible label a ``select`` step wants to choose. Pass it and
    a plausible-but-wrong dropdown is rejected here, cheaply, with a reason the
    re-prompt can act on; omit it and only the element itself is checked. It has
    to stay optional because ``reuse_is_valid`` validates a ``CachedAction``,
    which carries no option label — the wanted option lives in the scenario step.
    The check applies exactly where ``select.options`` is what execution searches;
    see :func:`_is_page_enhanced` for the one control class it does not.
    """

    if action not in ("click", "hover", "type", "waitFor", "select"):
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

        if action == "select":
            # A page may hide the real <select> and render its own dropdown
            # (select2 clips it to 1x1 px — already "visible" by Playwright's
            # rule below; Tom Select sets display:none, which is not). What
            # matters for `select` is whether the viewer sees *some* control
            # for it, not whether this exact element is on screen.
            control = await user_visible_control(locator)
            if control is None:
                return ValidationFail("not_visible", "The matched element is not visible.")
            # Only its existence was the question; the handle would otherwise
            # pin the element for the life of the context (see the ownership
            # note on `associated_control`).
            await control.dispose()
        elif not await locator.is_visible():
            return ValidationFail("not_visible", "The matched element is not visible.")

        if action == "type" and not await _is_type_compatible(locator):
            return ValidationFail(
                "incompatible_type", "The type action requires a text-entry element."
            )

        if action == "select" and not await _is_native_select(locator):
            return ValidationFail(
                "not_select", "The select action requires a native <select> element."
            )

        if action in ("click", "type", "select") and not await locator.is_enabled():
            return ValidationFail("not_enabled", "The matched element is disabled.")

        if action == "type" and not await locator.is_editable():
            return ValidationFail("not_editable", "The matched element is not editable.")

        # Where a select has no accessible name the resolver can only freeze it
        # positionally (combobox nth=N), and that index drifts with the DOM. The
        # wanted option is the semantic check that catches the drift — without it
        # a wrong-but-plausible dropdown only fails later, as a 15s timeout.
        if action == "select" and option is not None and not await _is_page_enhanced(locator):
            labels = await _select_option_labels(locator)
            if not _offers_option(labels, option):
                return ValidationFail("option_missing", _option_missing_message(option, labels))

        # Close the largest count/check race window. The DOM can always mutate
        # after this function returns, so execution performs its own checks too.
        if await locator.count() != 1:
            return ValidationFail("dom_changed", "The target changed while it was being validated.")
        return ValidationOk(locator)
    except PlaywrightError:
        return ValidationFail("dom_changed", "The target changed while it was being validated.")


async def reuse_failure(
    page: Page | Frame, cached: CachedAction, option: str | None = None
) -> ReuseReason | None:
    """Return why a frozen, previously resolved action can no longer be reused.

    ``None`` means the cached entry is safe to reuse as-is. Otherwise, the
    returned reason is the first check that failed, in the same order as the
    original ``reuse_is_valid`` boolean check performed them.

    ``option`` is the visible label a ``select`` step wants to choose, forwarded
    to :func:`validate_compile_time`. A ``CachedAction`` cannot supply it — the
    label lives in the scenario step, not the sidecar — so it stays optional for
    the :func:`reuse_is_valid` callers, which have no step in hand. ``guide``
    does, and passing it is what turns a 15s ``select_option`` timeout into an
    ``option_missing`` the caller can put in a sentence.
    """

    try:
        if cached.action == "waitFor":
            if cached.state is None:
                return "no_wait_state"
            if cached.state == "hidden":
                # A hidden wait intentionally has no identity at the point it
                # succeeds. Zero matches already satisfies the condition; one
                # match is safe to wait on; multiple matches are ambiguous.
                # This is a *success* path, so it must return here and never
                # fall through to the identity checks below: a hidden wait's
                # cached entry never carries an identity by design (see
                # ``models/action.py`` on ``CachedAction.identity``), so
                # falling through would misreport every valid hidden-wait
                # reuse as "identity_missing".
                locator = await build_locator(page, cached.target)
                return None if await locator.count() <= 1 else "wait_ambiguous"

        result = await validate_compile_time(page, cached.target, cached.action, option=option)
        if isinstance(result, ValidationFail):
            return result.reason
        if cached.identity is None:
            return "identity_missing"
        if (
            cached.action == "type"
            and cached.fingerprint.command_kind == "teach"
            and await is_sensitive_type_target(result.locator)
        ):
            return "sensitive_target"
        current_identity = await capture_identity(result.locator)
    except (PlaywrightError, ValueError):
        # The DOM may change between the locator checks and identity capture.
        return "dom_changed"
    return None if cached.identity.matches(current_identity) else "identity_mismatch"


async def reuse_is_valid(page: Page | Frame, cached: CachedAction) -> bool:
    """Validate a cached target and its independent, frozen identity.

    Thin boolean wrapper around :func:`reuse_failure`, kept for its existing
    callers (``recorder/render.py`` and ``recorder/compile.py``), which only
    need a yes/no answer and must not observe any behavior change.
    """

    return await reuse_failure(page, cached) is None
