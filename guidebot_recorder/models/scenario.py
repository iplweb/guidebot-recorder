"""Scenario and Step (§3) — validates "exactly one command per step".

The source scenario carries intents only; resolved actions live in a separate
CompiledScenario (``*.compiled.yaml``), not inline on the step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from guidebot_recorder.models.action import WaitState
from guidebot_recorder.models.config import Config, HighlightConfig

if TYPE_CHECKING:  # tylko dla typów — import w runtime zapętliłby się przez `scenario/__init__`
    from guidebot_recorder.scenario.source import ScenarioSource, StepLocation


class StepPathError(ValueError):
    """Błąd walidacji, który sam wie, którego kroku dotyczy.

    ``path`` to ścieżka **pozycyjna** w liście ``steps:`` — ``(3,)`` dla kroku
    top-level, ``(3, 1)`` dla drugiego dziecka bloku ``when:`` z pozycji 3.
    Walidatory poziomu ``Scenario`` mają ``loc == ()``, więc bez tego pola nie
    dałoby się przypisać ich komunikatu do konkretnej linii pliku.
    """

    def __init__(self, message: str, path: tuple[int, ...]) -> None:
        super().__init__(message)
        self.path = path


#: "primary" commands (an action/step); `say` may accompany one as narration
PRIMARY_COMMANDS = (
    "teach",
    "navigate",
    "click",
    "hover",
    "enter_text",
    "select",
    "highlight",
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


class ResolvedHighlight(NamedTuple):
    """A :class:`Highlight` with every knob filled in from ``config.highlight``."""

    what: str
    padding: float
    loops: int
    hold: float
    color: str


class Highlight(BaseModel):
    """Draw attention to a control or an area without touching it.

    ``what`` is the semantic target — a control ("przycisk Zapisz") or a whole
    region ("tabela z wynikami"). An area resolves like anything else only because
    this command widens the Reasoner's candidate set with container roles
    (``page_context.HIGHLIGHT_CANDIDATE_ROLES``); the acting commands still see
    controls alone. In the film the cursor laps an ellipse around the
    target, leaving a marker trail behind it; in the PDF guide the same ellipse is
    drawn onto the screenshot. Nothing is clicked, hovered or typed: this is the
    one command that points at the page without changing it.

    The knobs are ``None`` when the step says nothing about them — :meth:`resolved`
    fills them from ``config.highlight``, so the inheritance rule lives in exactly
    one place instead of being re-derived by the film and the guide.
    """

    model_config = ConfigDict(extra="forbid")
    what: str
    padding: float | None = Field(default=None, ge=0)
    loops: int | None = Field(default=None, ge=1, le=5)
    hold: float | None = Field(default=None, ge=0)
    color: str | None = None

    @field_validator("what")
    @classmethod
    def _what_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("`highlight` bez celu — podaj, co zakreślić")
        return value

    def resolved(self, defaults: HighlightConfig) -> ResolvedHighlight:
        """Merge this step's overrides onto the film-wide defaults."""

        return ResolvedHighlight(
            what=self.what,
            padding=defaults.padding if self.padding is None else self.padding,
            loops=defaults.loops if self.loops is None else self.loops,
            hold=defaults.hold if self.hold is None else self.hold,
            color=defaults.color if self.color is None else self.color,
        )


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
    #: richer per-step text for the PDF guide; overrides narration in `guide`,
    #: ignored by the video renderer. Not a command (does not count toward
    #: "exactly one command"); a step with only `caption` is still an empty step.
    caption: str | None = None
    teach: str | None = None
    navigate: str | NavigateConfig | None = None
    click: str | None = None
    hover: str | None = None
    enter_text: EnterText | None = Field(default=None, alias="enterText")
    select: Select | None = None
    highlight: Highlight | None = None
    scroll: Literal["up", "down", "top", "bottom"] | Scroll | None = None
    wait: float | WaitUntil | None = None
    slide: Slide | None = None
    desktop: Desktop | None = None
    #: close the active window and return to the one that opened it; `Literal[True]`
    #: so that `closeWindow: false` is a validation error rather than a silent no-op
    close_window: Literal[True] | None = Field(default=None, alias="closeWindow")
    #: tolerate an absent element instead of failing the run (single-step shorthand)
    optional: bool = False
    translations: dict[str, str] = Field(default_factory=dict)

    @field_validator("highlight", mode="before")
    @classmethod
    def _accept_bare_target(cls, value: Any) -> Any:
        """``highlight: "przycisk"`` is shorthand for ``highlight: {what: "przycisk"}``.

        Normalizing here rather than in an accessor (the way ``scroll_config``
        does it) is deliberate: a blank or malformed target then fails while the
        file is being loaded, where diagnostics can point at `plik:linia` and
        quote the offending YAML.
        """

        return {"what": value} if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def _reject_authored_expect(cls, data: Any) -> Any:
        """``expect:`` looks like an authoring control but nothing ever reads it here.

        Readiness (`navigation`/`idle`/`none`) is derived at compile time by
        ``heuristic_expect(url_before, url_after)`` from what the action was
        *observed* to do, and that result — not anything an author writes — is
        what gets frozen into the compiled sidecar's ``CachedAction``/
        ``Fingerprint`` (the only real ``.expect`` reads in the package). A
        ``Step.expect`` field used to accept the key and silently discard it;
        rejecting it here (rather than just deleting the field and letting
        ``extra="forbid"`` produce a generic "extra inputs" error) lets the
        message explain *why* the key is pointless and what to delete, the same
        way ``close_window``'s ``Literal[True]`` turns ``closeWindow: false``
        into a loud error instead of a silent no-op.
        """

        if isinstance(data, Mapping) and "expect" in data:
            raise ValueError(
                "`expect:` na kroku nic nie robi — gotowość (navigation/idle/none) "
                "wylicza kompilator z obserwacji tego, co akcja faktycznie zrobiła "
                "(zmiana URL), i zapisuje wynik w sidecarze (*.compiled.yaml); "
                "usuń `expect` z tego kroku — bez niego zachowanie się nie zmieni"
            )
        return data

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
        if kind in ("teach", "enterText", "click", "hover", "select", "highlight"):
            return True
        if kind == "wait" and isinstance(self.wait, WaitUntil):
            return True
        return False

    def highlight_config(self) -> Highlight:
        """The step's :class:`Highlight`; call only on a ``highlight`` step."""

        assert self.highlight is not None  # guaranteed by command_kind()
        return self.highlight

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
    #: where the step sits in the source YAML; None when the scenario was built
    #: in code (no source to point at) — diagnostics degrade to a bare number
    location: StepLocation | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    steps: list[Step | WhenBlock]

    #: mapa źródłowego YAML-a, doczepiana przez ``load_scenario``; ``PrivateAttr``,
    #: więc schemat, ``extra="forbid"`` i serializacja zostają bez zmian
    _source: ScenarioSource | None = PrivateAttr(default=None)

    @property
    def source(self) -> ScenarioSource | None:
        """Mapa źródła, jeśli scenariusz powstał z pliku."""

        return self._source

    def attach_source(self, source: ScenarioSource | None) -> None:
        """Doczep mapę źródła — po walidacji, bo ``model_validate`` jej nie przyjmie."""

        self._source = source

    def flat_steps(self) -> list[FlatStep]:
        """Flatten blocks into a linear list positionally aligned with compiled actions.

        Each ``WhenBlock`` contributes its gate step followed by its children, so
        the whole list can be indexed 1:1 by ``CompiledScenario.actions`` — and,
        when a source is attached, by ``ScenarioSource.steps``.
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
        if self._source is None:
            return flat
        return [
            entry._replace(location=self._source.location(index))
            for index, entry in enumerate(flat)
        ]

    @model_validator(mode="before")
    @classmethod
    def _reject_nested_blocks(cls, data: Any) -> Any:
        """Odrzuć zagnieżdżony blok ``when:`` — wskazując dziecko, nie rodzica.

        Sprawdzenie żyje tu, a nie na :class:`WhenBlock`, bo dopiero stąd widać
        pozycję bloku-rodzica: :class:`StepPathError` z parą ``(i, j)`` daje
        diagnostyce linię *zagnieżdżonego* ``when:`` i jeden, spójny numer kroku
        w nagłówku. Wariant na ``WhenBlock`` musiał doklejać do treści własne,
        lokalne ``krok {j}:`` — sprzeczne z 1-based numeracją nagłówka.

        Musi być ``mode="before"``: ``WhenBlock.steps`` to ``list[Step]``
        z ``extra="forbid"``, więc bez tej bramki autor dostałby zamiast
        komunikatu ścianę „Extra inputs are not permitted: steps.0.when".
        """

        if not isinstance(data, Mapping):
            return data
        entries = data.get("steps")
        if not isinstance(entries, list):
            return data
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping) or "when" not in entry:
                continue
            children = entry.get("steps")
            if not isinstance(children, list):
                continue
            for child_index, child in enumerate(children):
                if isinstance(child, Mapping) and child.get("when") is not None:
                    raise StepPathError(
                        "zagnieżdżony blok `when` nie jest wspierany", (index, child_index)
                    )
        return data

    @model_validator(mode="after")
    def _complete_audio_translations(self) -> Scenario:
        expected = {track.lang for track in self.config.audio_tracks}
        for index, entry in enumerate(self.steps):
            if isinstance(entry, WhenBlock):
                for child_index, child in enumerate(entry.steps):
                    _validate_translations(child, (index, child_index), expected)
            else:
                _validate_translations(entry, (index,), expected)
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

        Raises :class:`StepPathError`, not a bare ``ValueError``, and carries no
        ``krok {label}:`` prefix of its own: this validator lives on
        ``Scenario`` (``loc == ()``), so the positional path is the only thing
        that lets diagnostics turn the rejection into `plik:linia` plus the
        offending YAML fragment. A `select:` step that names the option but not
        the line to edit would be a worse message than the one a `click:` step
        in the same file already gets.
        """

        if self.config.selects.mode != "native":
            return self
        for index, entry in enumerate(self.steps):
            children = entry.steps if isinstance(entry, WhenBlock) else [entry]
            for child_index, child in enumerate(children):
                if child.select is None or child.select.mode != "shim":
                    continue
                path = (index, child_index) if isinstance(entry, WhenBlock) else (index,)
                raise StepPathError(
                    "`select.mode: shim` przy `config.selects.mode: native` "
                    "— nakładka nie jest wtedy w ogóle wstrzykiwana, więc nie ma czego "
                    "rozwinąć. Usuń `mode: shim` z kroku albo włącz nakładkę globalnie "
                    "(`config.selects.mode: shim`) i wyłącz ją per krok przez `mode: native`",
                    path,
                )
        return self


def _validate_translations(step: Step, path: tuple[int, ...], expected: set[str]) -> None:
    """Sprawdź tłumaczenia kroku spod ścieżki pozycyjnej ``path`` w ``steps:``.

    Treść nie zawiera numeru kroku — ``path`` niesie go w :class:`StepPathError`,
    a diagnostyka zamienia go na `plik:linia` w nagłówku bannera. Walidator żyje
    na poziomie ``Scenario`` (``loc == ()``), więc bez ``path`` komunikatu nie
    dałoby się przypiąć do żadnej linii pliku.
    """

    actual = set(step.translations)
    if step.narration() is None:
        if actual:
            languages = ", ".join(sorted(actual))
            raise StepPathError(f"tłumaczenia bez narracji `say`/`teach`: {languages}", path)
        return
    missing = expected - actual
    if missing:
        languages = ", ".join(sorted(missing))
        raise StepPathError(f"brak tłumaczeń dla ścieżek: {languages}", path)
    unknown = actual - expected
    if unknown:
        languages = ", ".join(sorted(unknown))
        raise StepPathError(f"niezdefiniowane tłumaczenia: {languages}", path)


def select_mode(step: Step, cfg: Config) -> str:
    """The effective select mode for one step (spec §5).

    A per-step ``mode`` is the escape hatch for one stubborn widget in an
    otherwise fine scenario, so it wins over ``config.selects.mode``; unset
    (``None``) inherits the global setting.

    Lives with the two models it reads rather than with either phase that
    dispatches on it: it is a pure lookup over ``Step`` and ``Config``, and
    homing it in the compile package made the render package import the compiler
    for it.
    """

    if step.select is not None and step.select.mode is not None:
        return step.select.mode
    return cfg.selects.mode
