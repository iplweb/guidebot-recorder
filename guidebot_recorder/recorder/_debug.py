"""Shared debugging conveniences for compile/render."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import quote, quote_plus

from playwright.async_api import Page

if TYPE_CHECKING:
    from guidebot_recorder.models.scenario import Scenario

_REDACTED = "<redacted>"


def scenario_sensitive_values(
    scenario: Scenario,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return input values and expanded navigation secrets for redaction."""

    values = {
        step.enter_text.text
        for step in scenario.steps
        if step.enter_text is not None and step.enter_text.text
    }
    navigation_urls = [url for step in scenario.steps if (url := step.navigate_url())]
    effective_env = os.environ if env is None else env
    for value in effective_env.values():
        if value and any(value in url for url in navigation_urls):
            values.add(value)
            values.update(url for url in navigation_urls if value in url)
    return tuple(values)


def redact_text(message: str, sensitive_values: Iterable[str]) -> str:
    """Remove raw, escaped, and URL-encoded sensitive values from diagnostics."""

    forms: set[str] = set()
    for value in sensitive_values:
        if not value:
            continue
        forms.add(value)
        forms.add(json.dumps(value, ensure_ascii=False)[1:-1])
        forms.add(quote(value, safe=""))
        forms.add(quote_plus(value, safe=""))
    if not forms:
        return message
    pattern = re.compile("|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True)))
    return pattern.sub(_REDACTED, message)


def redact_exception(exc: Exception, sensitive_values: Iterable[str]) -> str:
    """Format an exception without exposing scenario-sensitive values."""

    return redact_text(str(exc), sensitive_values)


async def pause_for_inspection(
    page: Page,
    phase: str,
    index: int,
    kind: str,
    exc: Exception,
    sensitive_values: Iterable[str] = (),
) -> None:
    """Pause and leave the window open for inspection (headed). Does not mask the error."""
    message = redact_exception(exc, sensitive_values)
    print(
        f"\n⏸  {phase}: krok {index + 1} ({kind}) padł: {type(exc).__name__}: {message}\n"
        "   Okno przeglądarki jest otwarte — obejrzyj stronę/DOM. Kliknij ▶ Resume\n"
        "   w panelu Playwright Inspector, aby kontynuować (błąd i tak zostanie zgłoszony).",
        flush=True,
    )
    try:
        await page.pause()
    except Exception:  # noqa: BLE001 — the pause is a convenience; it must not mask the step's error
        pass
