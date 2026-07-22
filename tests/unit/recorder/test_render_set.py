from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import guidebot_recorder.recorder.render_set as render_set_module
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.render_set import (
    RenderSetError,
    ensure_render_set_compiled,
    render_set_output_paths,
    render_set_up_to_date,
    run_compile_set,
    run_render_set,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.render_set import RenderSetPlan, load_render_set

_VARIANTS = (
    ("pl-PL", "pol", "login.pl.scenario.yaml", "login.pl.mp4"),
    ("en-US", "eng", "login.en.scenario.yaml", "login.en.mp4"),
    ("de-DE", "deu", "login.de.scenario.yaml", "login.de.mp4"),
)


def _plan(tmp_path: Path) -> RenderSetPlan:
    for language, mp4_language, scenario_name, _output_name in _VARIANTS:
        (tmp_path / scenario_name).write_text(
            textwrap.dedent(
                f"""\
                config:
                  title: Film {language}
                  viewport: {{width: 800, height: 600}}
                  locale: {language}
                  tts:
                    provider: edge
                    voice: voice-{language}
                    lang: {language}
                    trackLanguage: {mp4_language}
                steps:
                  - say: Narration {language}.
                """
            ),
            encoding="utf-8",
        )

    variants = "\n".join(
        textwrap.indent(
            textwrap.dedent(
                f"""\
                {language}:
                  scenario: {scenario_name}
                  output: {output_name}
                """
            ).rstrip(),
            "  ",
        )
        for language, _mp4_language, scenario_name, output_name in _VARIANTS
    )
    manifest = tmp_path / "login.render-set.yaml"
    manifest.write_text(
        "kind: localized-render-set\nversion: 1\nvariants:\n" + variants + "\n",
        encoding="utf-8",
    )
    return load_render_set(manifest)


async def test_render_set_stale_preflight_touches_no_runtime_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    out_dir = tmp_path / "out"
    checked: list[str] = []

    def compiled(path: Path, env=None) -> bool:  # noqa: ANN001
        checked.append(path.name)
        return path.name != "login.en.scenario.yaml"

    async def forbidden_render(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise AssertionError("render/browser/provider must not be touched before preflight")

    monkeypatch.setattr(render_set_module, "compile_up_to_date", compiled)
    monkeypatch.setattr(render_set_module, "run_render", forbidden_render)

    with pytest.raises(RenderSetError, match=r"en-US.*compile-set"):
        await run_render_set(
            plan,
            out_dir,
            object(),  # type: ignore[arg-type] -- must remain opaque during preflight
            tmp_path / "cache",
            object(),  # type: ignore[arg-type] -- must remain opaque during preflight
        )

    assert checked == [variant[2] for variant in _VARIANTS]
    assert not out_dir.exists()
    assert not (tmp_path / "cache").exists()


async def test_render_set_wraps_second_failure_and_does_not_start_third(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    failed_prior = out_dir / "login.en.mp4"
    not_started_prior = out_dir / "login.de.mp4"
    failed_prior.write_bytes(b"prior-en")
    not_started_prior.write_bytes(b"prior-de")
    started: list[str] = []

    monkeypatch.setattr(render_set_module, "compile_up_to_date", lambda path, env=None: True)

    async def render(path: Path, output: Path, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        started.append(path.name)
        if path.name == "login.en.scenario.yaml":
            raise RuntimeError("synthetic TTS failure")
        if path.name == "login.de.scenario.yaml":  # pragma: no cover - contract assertion
            raise AssertionError("third variant must not start")
        output.write_bytes(b"complete-pl")

    monkeypatch.setattr(render_set_module, "run_render", render)

    with pytest.raises(RenderSetError, match=r"render wariantu en-US: synthetic TTS failure"):
        await run_render_set(
            plan,
            out_dir,
            object(),  # type: ignore[arg-type]
            tmp_path / "cache",
            object(),  # type: ignore[arg-type]
        )

    assert started == ["login.pl.scenario.yaml", "login.en.scenario.yaml"]
    assert (out_dir / "login.pl.mp4").read_bytes() == b"complete-pl"
    assert failed_prior.read_bytes() == b"prior-en"
    assert not_started_prior.read_bytes() == b"prior-de"


async def test_compile_set_reuses_and_compiles_in_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    browser = object()
    reasoner = object()
    env = {"DEMO": "value"}
    freshness_checks: list[tuple[str, object]] = []
    compiled_calls: list[tuple[str, object, object, object, dict]] = []

    def compiled(path: Path, received_env=None) -> bool:  # noqa: ANN001
        freshness_checks.append((path.name, received_env))
        return path.name == "login.en.scenario.yaml"

    async def compile_in_browser(
        path: Path,
        received_browser,
        received_reasoner,
        received_env=None,
        **kwargs,
    ) -> None:  # noqa: ANN001
        compiled_calls.append(
            (path.name, received_browser, received_reasoner, received_env, kwargs)
        )

    monkeypatch.setattr(render_set_module, "compile_up_to_date", compiled)
    monkeypatch.setattr(render_set_module, "run_compile_in_browser", compile_in_browser)

    result = await run_compile_set(
        plan,
        browser,  # type: ignore[arg-type]
        reasoner,  # type: ignore[arg-type]
        env,
        timeout=12.5,
        pause_on_error=True,
        verbose=True,
    )

    assert freshness_checks == [(variant[2], env) for variant in _VARIANTS]
    assert [call[0] for call in compiled_calls] == [
        "login.pl.scenario.yaml",
        "login.de.scenario.yaml",
    ]
    assert all(
        call[1] is browser and call[2] is reasoner and call[3] is env for call in compiled_calls
    )
    assert [call[4] for call in compiled_calls] == [
        {"timeout": 12.5, "force": False, "pause_on_error": True, "verbose": True},
        {"timeout": 12.5, "force": False, "pause_on_error": True, "verbose": True},
    ]
    assert result.compiled == ("pl-PL", "de-DE")
    assert result.reused == ("en-US",)


def test_render_set_rejects_output_inside_another_variants_workspace(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    first, second = plan.variants[:2]
    colliding = replace(
        plan,
        variants=(
            replace(first, output=Path("foo.mp4.mp4")),
            replace(second, output=Path(".guidebot_video/foo.mp4")),
        ),
    )

    with pytest.raises(RenderSetError, match="katalogiem roboczym"):
        render_set_output_paths(colliding, tmp_path / "out")


def test_render_set_rejects_output_symlink_escape_and_alias_collision(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    out_dir = tmp_path / "out"
    real = out_dir / "real"
    outside = tmp_path / "outside"
    real.mkdir(parents=True)
    outside.mkdir()
    (out_dir / "alias").symlink_to(real, target_is_directory=True)
    (out_dir / "escape").symlink_to(outside, target_is_directory=True)

    escaping = replace(
        plan,
        variants=(replace(plan.variants[0], output=Path("escape/login.mp4")),),
    )
    with pytest.raises(RenderSetError, match="wychodzi poza --output-dir"):
        render_set_output_paths(escaping, out_dir)

    aliased = replace(
        plan,
        variants=(
            replace(plan.variants[0], output=Path("real/login.mp4")),
            replace(plan.variants[1], output=Path("alias/login.mp4")),
        ),
    )
    with pytest.raises(RenderSetError, match="kolidują"):
        render_set_output_paths(aliased, out_dir)


async def test_render_set_redacts_enter_text_value_from_wrapped_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    secret = "sentinel-runtime-secret"
    first = plan.variants[0]
    first.scenario.write_text(
        textwrap.dedent(
            """\
            config:
              title: Film pl-PL
              viewport: {width: 800, height: 600}
              locale: pl-PL
              tts: {provider: edge, voice: voice-pl-PL, lang: pl-PL, trackLanguage: pol}
            steps:
              - enterText: {into: Pole e-mail, text: "${PASSWORD}"}
            """
        ),
        encoding="utf-8",
    )
    one_variant = replace(plan, variants=(first,))

    monkeypatch.setattr(render_set_module, "compile_up_to_date", lambda path, env=None: True)

    async def fail_with_secret(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError(f'locator.fill("{secret}") timed out')

    monkeypatch.setattr(render_set_module, "run_render", fail_with_secret)

    with pytest.raises(RenderSetError) as captured:
        await run_render_set(
            one_variant,
            tmp_path / "out",
            object(),  # type: ignore[arg-type]
            tmp_path / "cache",
            object(),  # type: ignore[arg-type]
            {"PASSWORD": secret},
        )

    assert secret not in str(captured.value)
    assert "<redacted>" in str(captured.value)


#: Wariant, którego jedyny krok celowany trafia w dwa identyczne przyciski —
#: `compile` zmierzy indeks i zamrozi `nth`. Adres `data:` wystarczy, bo cały
#: dowód dotyczy sidecara, nie sieci.
_AMBIGUOUS_VARIANT = textwrap.dedent(
    """\
    config:
      title: Dwa przyciski
      viewport: {width: 800, height: 600}
      locale: pl-PL
      tts: {provider: edge, voice: v, lang: pl-PL, trackLanguage: pol}
    steps:
      - navigate: "data:text/html,<div><button>Usun</button></div><div><i>x</i></div><div><button>Usun</button></div>"
      - teach: "kliknij drugi Usun"
    """
)


class _PickingReasoner:
    """Wskazuje ostatniego kandydata o roli `button`; indeks liczy `compile`."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, instruction, candidates, feedback=None):  # noqa: ANN001, ANN201
        self.calls += 1
        buttons = [candidate for candidate in candidates if candidate.role == "button"]
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Usun", exact=True),
            candidate_id=buttons[-1].id,
        )


def _ambiguous_plan(tmp_path: Path) -> RenderSetPlan:
    (tmp_path / "ambig.pl.scenario.yaml").write_text(_AMBIGUOUS_VARIANT, encoding="utf-8")
    manifest = tmp_path / "ambig.render-set.yaml"
    manifest.write_text(
        textwrap.dedent(
            """\
            kind: localized-render-set
            version: 1
            variants:
              pl-PL:
                scenario: ambig.pl.scenario.yaml
                output: ambig.pl.mp4
            """
        ),
        encoding="utf-8",
    )
    return load_render_set(manifest)


@pytest.fixture
async def browser():
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch(headless=True)
        yield launched
        await launched.close()


async def test_compile_set_leaves_a_positional_variant_ready_to_render(
    tmp_path: Path, browser
) -> None:
    """Świeżo zbudowany sidecar z `nth` musi przejść preflight `render-set`.

    Bramka „czy trzeba otworzyć przeglądarkę" i pytanie „czy sidecar odpowiada
    źródłu" to dwa różne pytania. Zlanie ich w jedno robiło pętlę nie do
    przerwania: `compile-set` kończył się sukcesem, a `render-set` i tak żądał
    „uruchom `guidebot compile-set`" — bo świeży sidecar nadal niesie `nth`.
    """

    plan = _ambiguous_plan(tmp_path)

    result = await run_compile_set(plan, browser, _PickingReasoner())

    assert result.compiled == ("pl-PL",)
    ensure_render_set_compiled(plan)  # nie wolno rzucić
    assert render_set_up_to_date(plan) is True


async def test_compile_set_reopens_the_browser_for_a_positional_variant(
    tmp_path: Path, browser
) -> None:
    """Regresja odwrotna: bramka *kompilacji* zestawu nadal sprawdza dryf.

    Odcisk kroku nie zmienia się od przebudowy strony, więc pominięcie
    przeglądarki zabiłoby wykrywanie dryfu na ścieżce, którą używa człowiek.
    """

    plan = _ambiguous_plan(tmp_path)
    await run_compile_set(plan, browser, _PickingReasoner())

    second = _PickingReasoner()
    result = await run_compile_set(plan, browser, second)

    assert result.compiled == ("pl-PL",)  # przeglądarka otwarta ponownie
    assert result.reused == ()
    assert second.calls == 0  # ale bez dryfu wpis zostaje wznowiony w środku
