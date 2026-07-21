"""Collect a compact, accessibility-oriented view of the current page."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4

from playwright.async_api import Error, Page


@dataclass
class Candidate:
    """An actionable element exposed to the resolver reasoner."""

    id: str
    role: str
    name: str
    tag: str
    bbox: tuple[float, float, float, float]
    visible: bool
    enabled: bool
    ancestry: list[tuple[str, str]]


#: Roles worth offering the Reasoner for a command that *acts* on an element.
#: Interactive controls plus headings — a heading is not clickable but is the
#: landmark authors describe positions by ("under Settings").
CANDIDATE_ROLES = (
    "button",
    "checkbox",
    "combobox",
    "gridcell",
    "heading",
    "link",
    "listbox",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "radio",
    "scrollbar",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
    "treeitem",
)

#: Container roles, offered ON TOP of :data:`CANDIDATE_ROLES` to the one command
#: that points at a region instead of operating a control: `highlight`. A table,
#: a form or a section is never clickable, so it has no business in the candidate
#: set of `click`/`type`/`select` — widening the set for every command would
#: change what the model picks for scenarios that already compile. Kept narrow on
#: purpose: these are the roles an author actually names ("the results table",
#: "the summary section"), not every landmark the ARIA spec knows.
_CONTAINER_ROLES = (
    "article",
    "figure",
    "form",
    "grid",
    "group",
    "img",
    "list",
    "region",
    "table",
)

#: What a `highlight` step resolves against.
HIGHLIGHT_CANDIDATE_ROLES = CANDIDATE_ROLES + _CONTAINER_ROLES

#: Command kinds whose candidate set differs from the default.
_ROLES_BY_KIND = {"highlight": HIGHLIGHT_CANDIDATE_ROLES}


def candidate_roles_for(kind: str) -> tuple[str, ...]:
    """The roles the Reasoner may choose from when resolving a ``kind`` step."""

    return _ROLES_BY_KIND.get(kind, CANDIDATE_ROLES)


_MARK_CANDIDATE_ROLE_SCRIPT = r"""
(elements, options) => {
  let roleMap = window[options.roleMapKey];
  if (!(roleMap instanceof WeakMap)) {
    roleMap = new WeakMap();
    window[options.roleMapKey] = roleMap;
  }
  for (const element of elements) {
    roleMap.set(element, options.role);
  }
}
"""


_CLEAR_ROLE_MAP_SCRIPT = "roleMapKey => { delete window[roleMapKey]; }"


# This runs once over Playwright's CSS locator result. Unlike querySelectorAll,
# Playwright locators also pierce open shadow roots, which keeps web-component
# controls in the candidate set.
_COLLECT_CANDIDATES_SCRIPT = r"""
(elements, options) => {
  const roleMap = window[options.roleMapKey];
  if (!(roleMap instanceof WeakMap)) return [];

  // First-recognized-token semantics matter for fallback role values, e.g.
  // role="future-role button". Keeping the full set here also prevents a
  // presentational role from accidentally falling through to an implicit one.
  const knownRoles = new Set([
    "alert", "alertdialog", "application", "article", "banner", "blockquote",
    "button", "caption", "cell", "checkbox", "code", "columnheader",
    "combobox", "complementary", "contentinfo", "definition", "deletion",
    "dialog", "directory", "document", "emphasis", "feed", "figure", "form",
    "generic", "grid", "gridcell", "group", "heading", "img", "insertion",
    "link", "list", "listbox", "listitem", "log", "main", "marquee", "math",
    "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio", "meter",
    "navigation", "none", "note", "option", "paragraph", "presentation",
    "progressbar", "radio", "radiogroup", "region", "row", "rowgroup",
    "rowheader", "scrollbar", "search", "searchbox", "separator", "slider",
    "spinbutton", "status", "strong", "subscript", "superscript", "switch",
    "tab", "table", "tablist", "tabpanel", "term", "textbox", "time", "timer",
    "toolbar", "tooltip", "tree", "treegrid", "treeitem",
  ]);

  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();

  const explicitRole = (element) => {
    const value = element.getAttribute("role");
    if (!value) return null;
    for (const token of value.toLowerCase().trim().split(/\s+/)) {
      if (knownRoles.has(token)) return token;
    }
    return null;
  };

  const implicitRole = (element) => {
    const tag = element.localName.toLowerCase();

    if (/^h[1-6]$/.test(tag)) return "heading";
    if ((tag === "a" || tag === "area") && element.hasAttribute("href")) {
      return "link";
    }
    if (tag === "button" || tag === "summary") return "button";

    if (tag === "input") {
      const type = (element.getAttribute("type") || "text").toLowerCase();
      if (type === "hidden") return null;
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") {
        return element.hasAttribute("list") ? "combobox" : "searchbox";
      }
      if (["button", "color", "file", "image", "reset", "submit"].includes(type)) {
        return "button";
      }
      if (["email", "password", "tel", "text", "url"].includes(type)) {
        return element.hasAttribute("list") ? "combobox" : "textbox";
      }
      return null;
    }

    if (tag === "select") {
      return element.multiple || element.size > 1 ? "listbox" : "combobox";
    }
    if (tag === "textarea" || element.isContentEditable) return "textbox";
    if (tag === "option") return "option";

    // Common structural roles are useful in ancestry even though they are not
    // candidates by themselves.
    const structuralRoles = {
      article: "article",
      aside: "complementary",
      body: "document",
      dialog: "dialog",
      fieldset: "group",
      figure: "figure",
      footer: "contentinfo",
      form: "form",
      header: "banner",
      hr: "separator",
      img: "img",
      li: "listitem",
      main: "main",
      nav: "navigation",
      ol: "list",
      progress: "progressbar",
      table: "table",
      tbody: "rowgroup",
      td: "cell",
      tfoot: "rowgroup",
      th: "columnheader",
      thead: "rowgroup",
      tr: "row",
      ul: "list",
    };
    if (tag === "section" &&
        (element.hasAttribute("aria-label") || element.hasAttribute("aria-labelledby"))) {
      return "region";
    }
    return structuralRoles[tag] || null;
  };

  const roleOf = (element) => explicitRole(element) ?? implicitRole(element);

  const composedParent = (element) => {
    if (element.parentElement) return element.parentElement;
    const root = element.getRootNode();
    return root && root.host instanceof Element ? root.host : null;
  };

  const isAccessibilityHidden = (element) => {
    for (let current = element; current; current = composedParent(current)) {
      if (current.getAttribute("aria-hidden") === "true" || current.hasAttribute("inert")) {
        return true;
      }
    }
    return false;
  };

  const nameHidden = (node) => {
    const style = getComputedStyle(node);
    return node.getAttribute("aria-hidden")?.toLowerCase() === "true" ||
      node.hasAttribute("hidden") || style.display === "none" ||
      style.visibility === "hidden" || style.visibility === "collapse" ||
      style.contentVisibility === "hidden";
  };

  const textAlternative = (
    node,
    visited = new Set(),
    allowHiddenRoot = false,
    inHiddenReference = false,
  ) => {
    if (!node || visited.has(node)) return "";
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
    if (!(node instanceof Element)) return "";
    visited.add(node);

    const hidden = nameHidden(node);
    if (hidden && !allowHiddenRoot && !inHiddenReference) return "";
    const childInHiddenReference = inHiddenReference || (hidden && allowHiddenRoot);

    const labelledBy = normalize(node.getAttribute("aria-labelledby"));
    if (labelledBy) {
      const root = node.getRootNode();
      const labels = labelledBy.split(" ").map((id) => {
        const reference = typeof root.getElementById === "function"
          ? root.getElementById(id)
          : node.ownerDocument.getElementById(id);
        return textAlternative(reference, visited, true);
      });
      const result = normalize(labels.join(" "));
      if (result) return result;
    }

    const ariaLabel = normalize(node.getAttribute("aria-label"));
    if (ariaLabel) return ariaLabel;
    if (node.localName === "img" ||
        (node.localName === "input" && node.type === "image")) {
      const alt = normalize(node.getAttribute("alt"));
      if (alt) return alt;
    }

    return normalize(Array.from(
      node.childNodes,
      (child) => textAlternative(child, visited, false, childInHiddenReference),
    ).join(""));
  };

  const accessibleName = (element, role) => {
    const labelledBy = normalize(element.getAttribute("aria-labelledby"));
    if (labelledBy) {
      const root = element.getRootNode();
      const name = normalize(labelledBy.split(" ").map((id) => {
        const reference = typeof root.getElementById === "function"
          ? root.getElementById(id)
          : element.ownerDocument.getElementById(id);
        return textAlternative(reference, new Set(), true);
      }).join(" "));
      if (name) return name;
    }

    const ariaLabel = normalize(element.getAttribute("aria-label"));
    if (ariaLabel) return ariaLabel;

    if (element.labels && element.labels.length) {
      const labelName = normalize(
        Array.from(element.labels, (label) => textAlternative(label)).join(" ")
      );
      if (labelName) return labelName;
    }

    if (element.localName === "input") {
      const type = (element.getAttribute("type") || "text").toLowerCase();
      if (type === "image") {
        return normalize(element.getAttribute("alt")) || "Submit";
      }
      if (["button", "reset", "submit"].includes(type)) {
        const value = normalize(element.value);
        if (value) return value;
        if (type === "reset") return "Reset";
        if (type === "submit") return "Submit";
      }
    }

    if (["button", "checkbox", "gridcell", "heading", "link", "menuitem",
         "menuitemcheckbox", "menuitemradio", "option", "radio", "switch",
         "tab", "treeitem"].includes(role)) {
      const contentName = textAlternative(element);
      if (contentName) return contentName;
    }

    const title = normalize(element.getAttribute("title"));
    if (title) return title;

    if (["combobox", "searchbox", "spinbutton", "textbox"].includes(role)) {
      return normalize(element.getAttribute("placeholder"));
    }
    return "";
  };

  const enabled = (element) => {
    if (element.matches(":disabled")) return false;
    for (let current = element; current; current = composedParent(current)) {
      if (current.getAttribute("aria-disabled") === "true" || current.hasAttribute("inert")) {
        return false;
      }
    }
    return true;
  };

  const ancestryOf = (element) => {
    const result = [];
    for (let current = composedParent(element); current && result.length < 6;
         current = composedParent(current)) {
      result.push([current.localName.toLowerCase(), roleOf(current) || ""]);
    }
    return result.reverse();
  };

  const nthOfTypeCache = new WeakMap();
  const nthOfType = (element) => {
    const cached = nthOfTypeCache.get(element);
    if (cached !== undefined) return cached;

    let index = 1;
    for (let sibling = element.previousElementSibling; sibling;
         sibling = sibling.previousElementSibling) {
      if (sibling.localName !== element.localName) continue;
      const siblingIndex = nthOfTypeCache.get(sibling);
      if (siblingIndex !== undefined) {
        index += siblingIndex;
        break;
      }
      index += 1;
    }
    nthOfTypeCache.set(element, index);
    return index;
  };

  const domPath = (element) => {
    const parts = [];
    let current = element;
    while (current) {
      const tag = current.localName.toLowerCase();
      const parent = current.parentElement;
      let segment = tag;
      if (parent) {
        segment += `:nth-of-type(${nthOfType(current)})`;
      }
      parts.push(segment);
      if (parent) {
        current = parent;
        continue;
      }
      const root = current.getRootNode();
      if (root && root.host instanceof Element) {
        parts.push("::shadow");
        current = root.host;
      } else {
        current = null;
      }
    }
    return parts.reverse().join(">");
  };

  const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
  const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
  const result = [];

  for (const element of elements) {
    const role = roleMap.get(element);
    if (!role || isAccessibilityHidden(element)) continue;

    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const visible = rect.width > 0 && rect.height > 0 &&
      style.display !== "none" && style.visibility !== "hidden" &&
      style.visibility !== "collapse";
    if (!visible) continue;

    if (options.viewportOnly) {
      const intersectsViewport = rect.right > 0 && rect.bottom > 0 &&
        rect.left < viewportWidth && rect.top < viewportHeight;
      if (!intersectsViewport) continue;
    }

    result.push({
      role,
      name: accessibleName(element, role),
      tag: element.localName.toLowerCase(),
      bbox: [rect.x, rect.y, rect.width, rect.height],
      visible,
      enabled: enabled(element),
      ancestry: ancestryOf(element),
      path: domPath(element),
    });
    if (result.length >= options.limit) break;
  }
  return result;
}
"""


async def collect_candidates(
    page: Page,
    viewport_only: bool = True,
    limit: int = 200,
    *,
    roles: Sequence[str] = CANDIDATE_ROLES,
) -> list[Candidate]:
    """Return the visible elements of ``roles`` in deterministic DOM order.

    ``viewport_only`` keeps only elements whose bounding rectangle intersects
    the viewport. Passing zero as ``limit`` avoids evaluating the page; a
    negative limit is almost certainly a caller error and is rejected.

    ``roles`` defaults to the interactive controls and headings every acting
    command resolves against; :data:`HIGHLIGHT_CANDIDATE_ROLES` widens it with
    containers for the one command that points at a region instead of operating
    a control (see :func:`candidate_roles_for`).
    """

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []

    role_map_key = f"__guidebot_candidate_roles_{uuid4().hex}"
    try:
        for role in roles:
            await page.get_by_role(role).evaluate_all(
                _MARK_CANDIDATE_ROLE_SCRIPT,
                {"roleMapKey": role_map_key, "role": role},
            )

        raw_candidates: list[dict[str, Any]] = await page.locator("*").evaluate_all(
            _COLLECT_CANDIDATES_SCRIPT,
            {
                "roleMapKey": role_map_key,
                "viewportOnly": viewport_only,
                "limit": limit,
            },
        )
    finally:
        with suppress(Error):
            await page.evaluate(_CLEAR_ROLE_MAP_SCRIPT, role_map_key)

    candidates: list[Candidate] = []
    for raw in raw_candidates:
        bbox = raw["bbox"]
        ancestry = raw["ancestry"]
        stable_id = "candidate-" + sha256(raw["path"].encode("utf-8")).hexdigest()[:16]
        candidates.append(
            Candidate(
                id=stable_id,
                role=str(raw["role"]),
                name=str(raw["name"]),
                tag=str(raw["tag"]),
                bbox=tuple(float(value) for value in bbox),
                visible=bool(raw["visible"]),
                enabled=bool(raw["enabled"]),
                ancestry=[(str(tag), str(role)) for tag, role in ancestry],
            )
        )
    return candidates
