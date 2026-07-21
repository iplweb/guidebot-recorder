"""Scenario and Step (§3) — validates "exactly one command per step".

The source scenario carries intents only; resolved actions live in a separate
CompiledScenario (``*.compiled.yaml``), not inline on the step.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.config import Config

#: "primary" commands (an action/step); `say` may accompany one as narration
PRIMARY_COMMANDS = (
    "teach",
    "navigate",
    "click",
    "hover",
    "enter_text",
    "select",
    "scroll",
    "wait",
    "slide",
    "close_window",
    "desktop",
)

#: Built-in generic browser icons for the ``desktop`` step. Deliberately NOT the
#: real browser logos — those are trademarks and this package is redistributable.
#: They are hand-drawn stand-ins whose names merely say which browser they evoke;
#: a scenario that wants a real logo points ``icon`` at its own file instead.
DESKTOP_ICON_ALIASES = {
    "chrome": "browser",
    "browser": "browser",
    "firefox": "flame",
    "flame": "flame",
    "iexplore": "legacy",
    "edge": "legacy",
    "legacy": "legacy",
    "globe": "globe",
}


class EnterText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: str
    text: str


class Select(BaseModel):
    """Choose an option from a native ``<select>`` dropdown.

    ``from_`` (written ``from`` in YAML) is the semantic target of the dropdown;
    ``option`` is the visible label of the option to pick. The shim (default ``mode``)
    makes the option list visible and DOM-based: the cursor travels to the control,
    the list unfurls downward, the cursor travels to the chosen option, and it is
    clicked — all visible on camera. The per-step ``mode`` override falls back to
    "native" if a page's enhanced widget cannot be driven: the cursor still
    travels to the control, but the list never unfurls — the value changes at
    once, the instant the cursor arrives.

    ``option`` is shown, never spoken, and is not env-substituted. The ``mode``
    (optional) defaults to ``config.selects.mode`` when unset.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    option: str
    mode: Literal["shim", "native"] | None = None


class Scroll(BaseModel):
    """Scroll the page — a render-only visual with no agent target.

    ``to`` picks the direction/destination; ``amount`` (pixels) tunes an up/down
    scroll and is ignored for ``top``/``bottom``. Use it to bring below-the-fold
    content into view — for example a live-preview ``<iframe>`` whose contents the
    resolver cannot target — so the recording still shows it. ``down`` without an
    ``amount`` scrolls by most of a viewport; the shorthand ``scroll: down`` is
    accepted for any of the four destinations.
    """

    model_config = ConfigDict(extra="forbid")
    to: Literal["up", "down", "top", "bottom"] = "down"
    amount: float | None = None


class WaitUntil(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: str
    state: WaitState = "visible"
    timeout: float = 10.0


class NavigateConfig(BaseModel):
    """Object form of ``navigate`` with an optional typing override."""

    model_config = ConfigDict(extra="forbid")

    url: str
    type: bool | None = None


class Slide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    subtitle: str | None = None
    notes: str | None = None
    hold: float = 2.5

    @model_validator(mode="after")
    def _at_least_one_text(self) -> Slide:
        if not any((self.title, self.subtitle, self.notes)):
            raise ValueError("slide wymaga co najmniej jednego z: title/subtitle/notes")
        return self


class Desktop(BaseModel):
    """A simulated desktop opener: cursor double-clicks a browser icon, window opens.

    Visual-only, like :class:`Slide` — resolves to no compiled action. ``icon`` is
    either a built-in name (see :data:`DESKTOP_ICON_ALIASES`) or a path to the
    scenario author's own image; ``label`` is the caption under the icon. The
    desktop background colour is a render setting (``config.desktop.color``), not
    a per-step field, so every desktop step in a film matches.
    """

    model_config = ConfigDict(extra="forbid")
    icon: str = "chrome"
    label: str = "Przeglądarka internetowa"
    hold: float = 1.0

    def is_builtin_icon(self) -> bool:
        return self.icon in DESKTOP_ICON_ALIASES


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    say: str | None = None
    teach: str | None = None
    navigate: str | NavigateConfig | None = None
    click: str | None = None
    hover: str | None = None
    enter_text: EnterText | None = Field(default=None, alias="enterText")
    select: Select | None = None
    scroll: Literal["up", "down", "top", "bottom"] | Scroll | None = None
    wait: float | WaitUntil | None = None
    slide: Slide | None = None
    desktop: Desktop | None = None
    #: close the active window and return to the one that opened it; `Literal[True]`
    #: so that `closeWindow: false` is a validation error rather than a silent no-op
    close_window: Literal[True] | None = Field(default=None, alias="closeWindow")
    expect: Expect | None = None
    #: tolerate an absent element instead of failing the run (single-step shorthand)
    optional: bool = False
    translations: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_command(self) -> Step:
        present = [c for c in PRIMARY_COMMANDS if getattr(self, c) is not None]
        if len(present) > 1:
            raise ValueError(
                f"krok ma {len(present)} komend ({present}); dozwolona dokładnie jedna"
            )
        if len(present) == 0 and self.say is None:
            raise ValueError("krok bez komendy i bez `say` — pusty krok")
        return self

    @model_validator(mode="after")
    def _optional_only_where_it_can_be_honoured(self) -> Step:
        """``optional`` promises tolerance we can only deliver where there is a target.

        Accepting it silently on `say` / `navigate` / `slide` would imply a
        guarantee that does not exist; a numeric ``wait`` is allowed because it
        rides along with the branch it belongs to.
        """

        if not self.optional:
            return self
        if self.requires_target() or isinstance(self.wait, int | float):
            return self
        raise ValueError(
            f"`optional: true` nie ma zastosowania do kroku `{self.command_kind()}`; "
            "dozwolone tylko dla kroków z celem lub liczbowego `wait`"
        )

    def command_kind(self) -> str:
        for c in PRIMARY_COMMANDS:
            if getattr(self, c) is not None:
                if c == "enter_text":
                    return "enterText"
                if c == "close_window":
                    return "closeWindow"
                return c
        return "say"

    def requires_target(self) -> bool:
        kind = self.command_kind()
        if kind in ("teach", "enterText", "click", "hover", "select"):
            return True
        if kind == "wait" and isinstance(self.wait, WaitUntil):
            return True
        return False

    def scroll_config(self) -> Scroll:
        """Normalize the string shorthand and object form to a :class:`Scroll`."""
        return self.scroll if isinstance(self.scroll, Scroll) else Scroll(to=self.scroll)

    def navigate_url(self) -> str | None:
        """Return the URL from either the legacy string or object form."""
        if isinstance(self.navigate, NavigateConfig):
            return self.navigate.url
        return self.navigate

    def navigate_type_override(self) -> bool | None:
        """Return the per-step typing override, if object ``navigate`` supplies one."""
        if isinstance(self.navigate, NavigateConfig):
            return self.navigate.type
        return None

    def narration(self) -> str | None:
        """Return the canonical/default narration without changing action intent."""

        if self.say:
            return self.say
        if self.teach:
            return self.teach
        return None


class WhenBlock(BaseModel):
    """A group of steps that runs only when ``when`` shows up on the page.

    Kept separate from :class:`Step` on purpose: ``Step`` declares
    ``extra="forbid"``, so hanging ``state`` / ``timeout`` / ``steps`` off it
    would expose those keys on every step kind.
    """

    model_config = ConfigDict(extra="forbid")

    #: natural-language description of the gating element
    when: str
    state: WaitState = "visible"
    timeout: float = 10.0
    #: plain steps only — branches do not nest
    steps: list[Step]

    @model_validator(mode="before")
    @classmethod
    def _reject_nested_blocks(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        children = data.get("steps")
        if not isinstance(children, list):
            return data
        for index, child in enumerate(children):
            nested = child.get("when") if isinstance(child, dict) else getattr(child, "when", None)
            if nested is not None:
                raise ValueError(f"krok {index}: zagnieżdżony blok `when` nie jest wspierany")
        return data

    def gate_step(self) -> Step:
        """Return the synthetic step the gate compiles and renders as."""

        return Step(wait=WaitUntil(until=self.when, state=self.state, timeout=self.timeout))


class FlatStep(NamedTuple):
    """A scenario step in execution order, with the branch it belongs to."""

    step: Step
    #: index of the owning ``WhenBlock`` in ``Scenario.steps``; None at top level
    branch: int | None
    #: True for the synthetic gate step that opens a branch
    is_gate: bool


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    steps: list[Step | WhenBlock]

    def flat_steps(self) -> list[FlatStep]:
        """Flatten blocks into a linear list positionally aligned with compiled actions.

        Each ``WhenBlock`` contributes its gate step followed by its children, so
        the whole list can be indexed 1:1 by ``CompiledScenario.actions``.
        """

        flat: list[FlatStep] = []
        for index, entry in enumerate(self.steps):
            if isinstance(entry, WhenBlock):
                flat.append(FlatStep(step=entry.gate_step(), branch=index, is_gate=True))
                flat.extend(
                    FlatStep(step=child, branch=index, is_gate=False) for child in entry.steps
                )
            else:
                flat.append(FlatStep(step=entry, branch=None, is_gate=False))
        return flat

    @model_validator(mode="after")
    def _complete_audio_translations(self) -> Scenario:
        expected = {track.lang for track in self.config.audio_tracks}
        for index, entry in enumerate(self.steps):
            if isinstance(entry, WhenBlock):
                for child_index, child in enumerate(entry.steps):
                    _validate_translations(child, f"{index}.{child_index}", expected)
            else:
                _validate_translations(entry, str(index), expected)
        return self

    @model_validator(mode="after")
    def _step_shim_mode_needs_a_global_shim(self) -> Scenario:
        """A step cannot opt *into* a shim the scenario never installs.

        ``config.selects.mode: native`` is not a per-step default a step may
        override in either direction — it decides whether the widget script is
        injected into the browser context at all (``install_selects`` returns
        ``None`` for it). Underneath it there is no shim to drive: the step
        would reach a page with a raw ``<select>``, no DOM option list, and an
        association heuristic that finds some unrelated sibling to click on
        camera before failing.

        Rejecting the combination here means the author learns while the
        scenario loads, rather than several minutes into an unattended render.
        """

        if self.config.selects.mode != "native":
            return self
        for index, entry in enumerate(self.steps):
            children = entry.steps if isinstance(entry, WhenBlock) else [entry]
            for child_index, child in enumerate(children):
                if child.select is None or child.select.mode != "shim":
                    continue
                label = f"{index}.{child_index}" if isinstance(entry, WhenBlock) else str(index)
                raise ValueError(
                    f"krok {label}: `select.mode: shim` przy `config.selects.mode: native` "
                    "— nakładka nie jest wtedy w ogóle wstrzykiwana, więc nie ma czego "
                    "rozwinąć. Usuń `mode: shim` z kroku albo włącz nakładkę globalnie "
                    "(`config.selects.mode: shim`) i wyłącz ją per krok przez `mode: native`"
                )
        return self


def _validate_translations(step: Step, label: str, expected: set[str]) -> None:
    actual = set(step.translations)
    if step.narration() is None:
        if actual:
            languages = ", ".join(sorted(actual))
            raise ValueError(f"krok {label}: tłumaczenia bez narracji `say`/`teach`: {languages}")
        return
    missing = expected - actual
    if missing:
        languages = ", ".join(sorted(missing))
        raise ValueError(f"krok {label}: brak tłumaczeń dla ścieżek: {languages}")
    unknown = actual - expected
    if unknown:
        languages = ", ".join(sorted(unknown))
        raise ValueError(f"krok {label}: niezdefiniowane tłumaczenia: {languages}")
