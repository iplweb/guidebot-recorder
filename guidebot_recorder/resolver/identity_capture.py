"""Capture the frozen structural identity of a resolved DOM element."""

from __future__ import annotations

import hashlib
import json
from typing import TypedDict

from playwright.async_api import Locator

from guidebot_recorder.models.identity import Identity


class _DomIdentity(TypedDict):
    tag: str
    testid: str | None
    href: str | None
    ancestry: list[list[str]]


_CAPTURE_SCRIPT = """
(element) => {
  const normalizeRole = (ancestor) => {
    const role = ancestor.getAttribute("role");
    return role === null
      ? ""
      : role.trim().toLowerCase().replace(/\\s+/g, " ");
  };

  const ancestry = [];
  for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
    ancestry.push([
      ancestor.tagName.toLowerCase(),
      normalizeRole(ancestor),
    ]);
  }

  const tag = element.tagName.toLowerCase();
  return {
    tag,
    testid: element.getAttribute("data-testid"),
    href: tag === "a" && element.hasAttribute("href") ? element.href : null,
    ancestry,
  };
}
"""


def _digest_ancestry(ancestry: list[list[str]]) -> str:
    """Hash an unambiguous, deterministic representation of ancestor pairs."""
    pairs = [(tag, role) for tag, role in ancestry]
    canonical = json.dumps(
        pairs,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def capture_identity(locator: Locator) -> Identity:
    """Capture identity for the locator's single matching DOM element.

    An identity intentionally excludes accessible name and role of the target:
    those fields form part of the locator and would make verification
    tautological. Ancestor roles remain part of the independent structural
    fingerprint.
    """
    count = await locator.count()
    if count != 1:
        raise ValueError(
            f"capture_identity requires exactly one matching element; got {count}"
        )

    captured: _DomIdentity = await locator.evaluate(_CAPTURE_SCRIPT)
    return Identity(
        tag=captured["tag"],
        testid=captured["testid"],
        href=captured["href"],
        ancestry_digest=_digest_ancestry(captured["ancestry"]),
    )
