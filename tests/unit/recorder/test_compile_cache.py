"""`compile` cache reuse, sidecar rewriting and mid-run checkpointing.

Reuse-*validity* checks (staleness of the compiler version / fingerprint,
positional-index drift) live in ``test_compile_reuse.py``.
"""

import textwrap

import pytest

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import compile_up_to_date, run_compile
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled

from ._compile_helpers import SCENARIO, MockReasoner, make_page


@pytest.fixture
async def page():
    async for pg in make_page():
        yield pg


async def test_compile_fills_cached_action(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    reasoner = MockReasoner()

    await run_compile(path, page, reasoner, selects=None)

    compiled = load_compiled(compiled_path(path))
    ca = compiled.actions[1]
    assert ca is not None
    assert ca.action == "click"
    assert isinstance(ca.target, RoleTarget)
    assert ca.target.name == "Zaloguj"
    assert ca.identity.tag == "button"
    assert ca.fingerprint.config_hash  # niepusty
    assert reasoner.calls == 1


async def test_expect_is_read_from_the_urls_bracketing_the_action(tmp_path, page):
    """`expect` is the *difference* between the URL before and after the action.

    Both readings have to stay on their own side of the click: `heuristic_expect`
    compares them and calls any change a navigation, so a `url_before` sampled
    after the action (or a `url_after` sampled before it) makes the two equal and
    freezes `expect: none` for a step that navigates. Render then skips the load
    wait and photographs the old document. Nothing else in the compile tests
    notices — every other scenario across the ``test_compile_*.py`` files clicks a
    button that leaves the URL alone, which is exactly the shape that survives the
    mistake.
    """

    path = tmp_path / "nawigacja.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Nawigacja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              # `about:blank`, not a fragment: Chromium treats a data: document
              # as opaque and a same-document hash click leaves `page.url` alone,
              # which is the one thing this test needs to change.
              - navigate: "data:text/html,<a href=about:blank>Dalej</a>"
              - teach: "kliknij Dalej"
            """
        ),
        encoding="utf-8",
    )

    class LinkReasoner:
        async def resolve(self, instruction, candidates):
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="link", name="Dalej", exact=True),
            )

    await run_compile(path, page, LinkReasoner(), selects=None)

    ca = load_compiled(compiled_path(path)).actions[1]
    assert ca is not None
    assert ca.expect == "navigation"
    # The fingerprint carries the same verdict, and `_can_reuse` cross-checks the
    # two — a wrong `expect` frozen consistently would still be reused forever.
    assert ca.fingerprint.expect == "navigation"


async def test_recompile_reuses_cache_without_reasoner(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    first = MockReasoner()
    await run_compile(path, page, first, selects=None)
    assert first.calls == 1

    second = MockReasoner()
    await run_compile(path, page, second, selects=None)
    assert second.calls == 0  # reuse — LLM nie wołany


async def test_recompile_reuses_cache_without_rewriting_unchanged_sidecar(
    tmp_path, page, monkeypatch
):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    writes = 0
    original_write = compile_module.run.write_compiled

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(compile_module.run, "write_compiled", count_write)

    await run_compile(path, page, MockReasoner(), selects=None)
    assert writes == 1  # fresh resolve checkpoint; navigate does not rewrite the sidecar

    writes = 0
    reasoner = MockReasoner()
    await run_compile(path, page, reasoner, selects=None)

    assert reasoner.calls == 0
    assert writes == 0


async def test_fresh_resolution_is_checkpointed_before_a_later_failure(tmp_path, page):
    path = tmp_path / "partial.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Częściowa kompilacja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<button>Pierwszy</button><button>Drugi</button>"
              - teach: "kliknij Pierwszy"
              - teach: "kliknij Drugi"
            """
        ),
        encoding="utf-8",
    )

    class FailsSecondResolution:
        async def resolve(self, instruction, candidates):
            if "Drugi" in instruction:
                raise RuntimeError("synthetic second-step failure")
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="button", name="Pierwszy", exact=True),
            )

    with pytest.raises(RuntimeError, match="synthetic second-step failure"):
        await run_compile(path, page, FailsSecondResolution(), selects=None)

    compiled = load_compiled(compiled_path(path))
    assert compiled.actions[1] is not None
    assert compiled.actions[2] is None


async def test_targetless_compile_still_writes_final_aligned_sidecar(tmp_path, page, monkeypatch):
    path = tmp_path / "narration.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Narracja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - say: "Cześć"
            """
        ),
        encoding="utf-8",
    )
    writes = 0
    original_write = compile_module.run.write_compiled

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(compile_module.run, "write_compiled", count_write)

    await run_compile(path, page, MockReasoner(), selects=None)

    compiled = load_compiled(compiled_path(path))
    assert compiled.actions == [None]
    assert writes == 1


async def test_empty_scenario_still_does_not_create_sidecar(tmp_path, page):
    path = tmp_path / "empty.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Pusty scenariusz
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps: []
            """
        ),
        encoding="utf-8",
    )

    await run_compile(path, page, MockReasoner(), force=True, selects=None)

    assert not compiled_path(path).exists()


async def test_slide_compiles_to_null_without_reasoner(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Slajd
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - slide:
              title: "Krok 1"
              subtitle: "Kliknij przycisk"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "slide.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    reasoner = MockReasoner()

    await run_compile(path, page, reasoner, selects=None)

    compiled = load_compiled(compiled_path(path))
    assert len(compiled.actions) == 3  # jeden slot na krok — również dla slide
    assert compiled.actions[1] is None  # slide → null cached action, bez Reasonera
    assert compiled.actions[2] is not None  # kolejny krok (teach) rozwiązany normalnie
    assert reasoner.calls == 1  # Reasoner wołany tylko dla kroku teach, nie dla slide


async def test_editing_translation_does_not_invalidate_canonical_teach(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Logowanie
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: pl, lang: pl-PL, trackLanguage: pol}
          audioTracks:
            - {provider: edge, voice: en, lang: en-US, trackLanguage: eng}
        steps:
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - teach: "kliknij Zaloguj"
            translations: {en-US: "Click Log in"}
        """
    )
    path = tmp_path / "multilingual.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    path.write_text(scenario.replace("Click Log in", "Choose Log in"), encoding="utf-8")

    assert compile_up_to_date(path) is True
    reasoner = MockReasoner()
    await run_compile(path, page, reasoner, selects=None)
    assert reasoner.calls == 0
    action = load_compiled(compiled_path(path)).actions[1]
    assert action is not None
    assert action.fingerprint.compiled_from == "kliknij Zaloguj"


async def test_compile_sets_viewport_from_config(tmp_path, page):
    # config.viewport = 800x600; compile MUSI go ustawić (spójność z render)
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    await run_compile(path, page, MockReasoner(), selects=None)

    assert page.viewport_size == {"width": 800, "height": 600}


async def test_compile_force_reresolves(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    first = MockReasoner()
    await run_compile(path, page, first, selects=None)
    assert first.calls == 1

    forced = MockReasoner()
    await run_compile(path, page, forced, force=True, selects=None)
    assert forced.calls == 1  # --force ignoruje cache i woła reasonera ponownie


async def test_compile_navigates_with_object_form_and_ignores_render_type_flag(tmp_path, page):
    path = tmp_path / "object-navigate.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Object navigate
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate:
                  url: "data:text/html,<h1>Object navigation</h1>"
                  type: true
            """
        ),
        encoding="utf-8",
    )
    reasoner = MockReasoner()

    await run_compile(path, page, reasoner, selects=None)

    assert await page.get_by_role("heading", name="Object navigation").count() == 1
    assert reasoner.calls == 0
