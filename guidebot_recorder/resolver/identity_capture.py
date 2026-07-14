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
  const concreteRoles = new Set(`
    alert alertdialog application article banner blockquote button caption cell
    checkbox code columnheader combobox complementary contentinfo definition
    deletion dialog directory document emphasis feed figure form generic grid
    gridcell group heading img insertion link list listbox listitem log main
    marquee math menu menubar menuitem menuitemcheckbox menuitemradio meter
    navigation none note option paragraph presentation progressbar radio
    radiogroup region row rowgroup rowheader scrollbar search searchbox separator
    slider spinbutton status strong subscript suggestion superscript switch tab
    table tablist tabpanel term textbox time timer toolbar tooltip tree treegrid
    treeitem
  `.trim().split(/\\s+/));

  const hasAccessibleNameHint = (node) => {
    const ariaLabel = node.getAttribute("aria-label");
    const labelledBy = node.getAttribute("aria-labelledby");
    return (ariaLabel !== null && ariaLabel.trim() !== "") ||
      (labelledBy !== null && labelledBy.trim() !== "") ||
      node.hasAttribute("title");
  };

  const implicitRole = (node) => {
    const tag = node.tagName.toLowerCase();

    if ((tag === "a" || tag === "area") && node.hasAttribute("href")) {
      return "link";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";

    switch (tag) {
      case "article": return "article";
      case "aside": return "complementary";
      case "blockquote": return "blockquote";
      case "button": return "button";
      case "code": return "code";
      case "datalist": return "listbox";
      case "del": return "deletion";
      case "details": return "group";
      case "dialog": return "dialog";
      case "em": return "emphasis";
      case "fieldset": return "group";
      case "figure": return "figure";
      case "form": return hasAccessibleNameHint(node) ? "form" : "";
      case "header":
        return node.closest("article, aside, main, nav, section")
          ? ""
          : "banner";
      case "footer":
        return node.closest("article, aside, main, nav, section")
          ? ""
          : "contentinfo";
      case "hr": return "separator";
      case "html": return "document";
      case "img": return node.getAttribute("alt") === "" ? "presentation" : "img";
      case "ins": return "insertion";
      case "li": return "listitem";
      case "main": return "main";
      case "menu":
      case "ol":
      case "ul": return "list";
      case "meter": return "meter";
      case "nav": return "navigation";
      case "optgroup": return "group";
      case "option": return "option";
      case "output": return "status";
      case "p": return "paragraph";
      case "progress": return "progressbar";
      case "search": return "search";
      case "section": return hasAccessibleNameHint(node) ? "region" : "";
      case "strong": return "strong";
      case "sub": return "subscript";
      case "summary": return "button";
      case "sup": return "superscript";
      case "table": return "table";
      case "tbody":
      case "tfoot":
      case "thead": return "rowgroup";
      case "td": return "cell";
      case "textarea": return "textbox";
      case "th": return node.getAttribute("scope") === "row" ? "rowheader" : "columnheader";
      case "time": return "time";
      case "tr": return "row";
      case "select":
        return node.multiple || node.size > 1 ? "listbox" : "combobox";
      case "input": {
        const type = (node.getAttribute("type") || "text").toLowerCase();
        if (["button", "image", "reset", "submit"].includes(type)) return "button";
        if (type === "checkbox") return "checkbox";
        if (type === "number") return "spinbutton";
        if (type === "radio") return "radio";
        if (type === "range") return "slider";
        if (type === "search") return "searchbox";
        if (["email", "tel", "text", "url"].includes(type)) return "textbox";
        return "";
      }
      default: return "";
    }
  };

  const effectiveRole = (node) => {
    const tokens = (node.getAttribute("role") || "")
      .toLowerCase()
      .trim()
      .split(/\\s+/)
      .filter(Boolean);
    for (const token of tokens) {
      if (concreteRoles.has(token)) return token;
    }
    return implicitRole(node);
  };

  const composedParent = (node) => {
    if (node.assignedSlot) return node.assignedSlot;
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode();
    return root instanceof ShadowRoot ? root.host : null;
  };

  const ancestry = [];
  for (
    let ancestor = composedParent(element);
    ancestor;
    ancestor = composedParent(ancestor)
  ) {
    ancestry.push([
      ancestor.tagName.toLowerCase(),
      effectiveRole(ancestor),
    ]);
  }

  const tag = element.tagName.toLowerCase();
  let href = null;
  if (["a", "area", "link"].includes(tag) && element.hasAttribute("href")) {
    try {
      href = new URL(element.getAttribute("href"), element.baseURI).href;
    } catch (_) {
      href = null;
    }
  }
  return {
    tag,
    testid: element.getAttribute("data-testid"),
    href,
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
