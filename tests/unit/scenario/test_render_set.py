from __future__ import annotations

import textwrap
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

import pytest

from guidebot_recorder.scenario.loader import ScenarioValidationError
from guidebot_recorder.scenario.render_set import RenderSetValidationError, load_render_set


def _scenario(
    language: str,
    track_language: str = "eng",
    *,
    locale: str | None = None,
    provider: str = "fake",
    audio_tracks: str = "",
) -> str:
    locale = language if locale is None else locale
    config = textwrap.dedent(
        f"""\
        config:
          title: {language}
          viewport: {{width: 640, height: 480}}
          locale: {locale}
          tts:
            provider: {provider}
            voice: voice-{language}
            lang: {language}
            trackLanguage: {track_language}
        """
    )
    if audio_tracks:
        config += textwrap.indent(textwrap.dedent(audio_tracks).strip(), "  ") + "\n"
    return config + f'steps:\n  - say: "Narration in {language}"\n'


def _write_set(tmp_path: Path, variants: str) -> Path:
    path = tmp_path / "localized.render-set.yaml"
    body = textwrap.indent(textwrap.dedent(variants).strip(), "  ")
    path.write_text(
        f"kind: localized-render-set\nversion: 1\nvariants:\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_load_render_set_preserves_order_and_resolves_non_secret_paths(tmp_path: Path) -> None:
    (tmp_path / "pl.scenario.yaml").write_text(
        _scenario("pl-PL", "pol").replace(
            '- say: "Narration in pl-PL"',
            '- enterText: {into: "pole e-mail", text: "${DEMO_EMAIL}"}',
        ),
        encoding="utf-8",
    )
    (tmp_path / "en.scenario.yaml").write_text(_scenario("en-US"), encoding="utf-8")
    manifest = _write_set(
        tmp_path,
        """\
              pl-PL: {scenario: pl.scenario.yaml, output: videos/login.pl.mp4}
              en-US: {scenario: en.scenario.yaml, output: videos/login.en.mp4}
        """,
    )

    plan = load_render_set(manifest, {"DEMO_EMAIL": "secret@example.test"})

    assert [variant.language for variant in plan.variants] == ["pl-PL", "en-US"]
    assert [variant.scenario.name for variant in plan.variants] == [
        "pl.scenario.yaml",
        "en.scenario.yaml",
    ]
    assert [variant.output.as_posix() for variant in plan.variants] == [
        "videos/login.pl.mp4",
        "videos/login.en.mp4",
    ]
    assert plan.source.is_absolute()
    assert all(variant.scenario.is_absolute() for variant in plan.variants)
    assert plan.provider == "fake"
    assert "secret@example.test" not in repr(plan)


def test_load_render_set_does_not_expose_substituted_secret_in_validation_error(
    tmp_path: Path,
) -> None:
    """Błąd wariantu pokazuje banner scenariusza, ale nigdy podstawionej wartości.

    Zestaw zawijał dawniej *każdy* błąd wczytania wariantu w
    ``RenderSetValidationError`` z samą nazwą typu, bo wypadał tędy surowy
    ``pydantic.ValidationError`` z ``input_value=…`` — czyli z wartościami już
    po podstawieniu ``${ENV}``. Dziś ``load_scenario`` zamienia go u źródła na
    banner, którego snippet pochodzi *sprzed* substytucji, więc banner może iść
    do użytkownika w całości: mówi, w którym pliku i w której linii jest błąd.
    Asercja na ``${PASSWORD}`` jest tu istotą dowodu — gdyby snippet powstawał
    po substytucji, stałaby w tym miejscu wartość sekretu.
    """

    secret = "sentinel-password-that-must-not-leak"
    (tmp_path / "en.scenario.yaml").write_text(
        _scenario("en-US").replace(
            '- say: "Narration in en-US"',
            '- enterText: {text: "${PASSWORD}"}',
        ),
        encoding="utf-8",
    )
    manifest = _write_set(
        tmp_path,
        "      en-US: {scenario: en.scenario.yaml, output: en.mp4}",
    )

    with pytest.raises(ScenarioValidationError) as captured:
        load_render_set(manifest, {"PASSWORD": secret})

    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert "${PASSWORD}" in str(captured.value)
    assert "en.scenario.yaml:" in str(captured.value)


def test_load_render_set_redacts_secret_from_non_validation_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wyjątek spoza ``ScenarioValidationError`` gubi treść, zostaje sama nazwa typu.

    To druga — dotąd niepokryta testem — gałąź `try`/`except` wokół
    ``load_scenario``. Siostrzany test powyżej pilnuje gałęzi
    ``ScenarioValidationError`` (banner, bezpieczny w całości); tutaj chodzi
    o wszystko inne, co może wypaść z wczytywania *po* podstawieniu ``${ENV}``:
    surowy ``pydantic.ValidationError`` z ``input_value=…``, ``OSError`` ze
    ścieżką, cokolwiek. Redakcja stoi na dwóch szczegółach, które łatwo zgubić
    przy przenoszeniu tej gałęzi do helpera: komunikat składa się wyłącznie
    z ``type(exc).__name__`` (nigdy ``str(exc)``), a ``raise ... from None``
    ucina łańcuch, żeby oryginał nie wypłynął w tracebacku do logów.
    Gdyby którykolwiek zniknął, sekret trafiłby do wyjścia i nic by nie padło.
    """

    secret = "sentinel-password-that-must-not-leak"

    def _fail_after_substitution(
        scenario_path: Path, env: Mapping[str, str] | None = None
    ) -> NoReturn:
        # Udajemy wyjątek, który — jak `pydantic.ValidationError` — niesie
        # w treści wartość już po podstawieniu ${ENV}, a nie samą nazwę klucza.
        assert env is not None
        raise RuntimeError(f"input_value={env['PASSWORD']!r}")

    monkeypatch.setattr(
        "guidebot_recorder.scenario.render_set.load_scenario",
        _fail_after_substitution,
    )
    (tmp_path / "en.scenario.yaml").write_text(
        _scenario("en-US").replace(
            '- say: "Narration in en-US"',
            '- enterText: {text: "${PASSWORD}"}',
        ),
        encoding="utf-8",
    )
    manifest = _write_set(
        tmp_path,
        "      en-US: {scenario: en.scenario.yaml, output: en.mp4}",
    )

    with pytest.raises(RenderSetValidationError) as captured:
        load_render_set(manifest, {"PASSWORD": secret})

    rendered = "".join(traceback.format_exception(captured.value))
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in rendered
    # `from None` — nie ma przyczyny do wypisania, a kontekst jest wygaszony,
    # więc oryginalny RuntimeError nie dojdzie do tracebacku ani do logów.
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    assert str(captured.value) == (
        "wariant en-US: nie można wczytać en.scenario.yaml "
        "(RuntimeError); sprawdź poprawność scenariusza"
    )


@pytest.mark.parametrize(
    ("scenario_path", "output_path", "message"),
    [
        ("/tmp/pl.scenario.yaml", "pl.mp4", "względną"),
        ("C:/tmp/pl.scenario.yaml", "pl.mp4", "względną"),
        ("C:pl.scenario.yaml", "pl.mp4", "względną"),
        ("pl.scenario.yaml", "foo:bar.mp4", "przenośnych"),
        ("../pl.scenario.yaml", "pl.mp4", "bez `..`"),
        ("pl.yaml", "pl.mp4", ".scenario.yaml"),
        ("pl.scenario.yaml", "/tmp/pl.mp4", "względną"),
        ("pl.scenario.yaml", "C:/tmp/pl.mp4", "względną"),
        ("pl.scenario.yaml", "../pl.mp4", "bez `..`"),
        ("pl.scenario.yaml", "pl.webm", ".mp4"),
    ],
)
def test_load_render_set_rejects_unsafe_or_wrong_suffix_paths(
    tmp_path: Path, scenario_path: str, output_path: str, message: str
) -> None:
    (tmp_path / "pl.scenario.yaml").write_text(_scenario("pl-PL", "pol"), encoding="utf-8")
    manifest = _write_set(
        tmp_path,
        f'      pl-PL: {{scenario: "{scenario_path}", output: "{output_path}"}}',
    )

    with pytest.raises(RenderSetValidationError, match=message):
        load_render_set(manifest)


@pytest.mark.parametrize(
    ("language", "scenario", "message"),
    [
        ("en-us", _scenario("en-us"), "BCP 47"),
        ("en-US", _scenario("en-US", locale="pl-PL"), "config.locale"),
        ("en-US", _scenario("pl-PL", "pol", locale="en-US"), "config.tts.lang"),
        (
            "en-US",
            _scenario(
                "en-US",
                audio_tracks=(
                    "audioTracks:\n"
                    "  - {provider: fake, voice: pl, lang: pl-PL, trackLanguage: pol}\n"
                ),
            ).replace(
                '- say: "Narration in en-US"',
                '- say: "Narration in en-US"\n    translations: {pl-PL: "Polska narracja"}',
            ),
            "dokładnie jednej",
        ),
    ],
)
def test_load_render_set_rejects_language_or_track_contract_mismatch(
    tmp_path: Path, language: str, scenario: str, message: str
) -> None:
    source = tmp_path / "variant.scenario.yaml"
    source.write_text(scenario, encoding="utf-8")
    manifest = _write_set(
        tmp_path,
        f"      {language}: {{scenario: variant.scenario.yaml, output: variant.mp4}}",
    )

    with pytest.raises((RenderSetValidationError, ValueError), match=message):
        load_render_set(manifest)


def test_load_render_set_requires_track_language(tmp_path: Path) -> None:
    source = tmp_path / "en.scenario.yaml"
    source.write_text(
        _scenario("en-US").replace("    trackLanguage: eng\n", ""),
        encoding="utf-8",
    )
    manifest = _write_set(
        tmp_path,
        "      en-US: {scenario: en.scenario.yaml, output: en.mp4}",
    )

    with pytest.raises(RenderSetValidationError, match="trackLanguage"):
        load_render_set(manifest)


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("kind: other\nversion: 1", "localized-render-set"),
        ("kind: localized-render-set\nversion: 1.0", "liczbą całkowitą"),
        ("kind: localized-render-set\nversion: 2", "Input should be 1"),
        ("kind: localized-render-set\nversion: 1\nextra: true", "Extra inputs"),
    ],
)
def test_load_render_set_rejects_wrong_kind_version_or_extra_field(
    tmp_path: Path, header: str, message: str
) -> None:
    (tmp_path / "en.scenario.yaml").write_text(_scenario("en-US"), encoding="utf-8")
    manifest = tmp_path / "localized.render-set.yaml"
    manifest.write_text(
        header + "\nvariants:\n" + "  en-US: {scenario: en.scenario.yaml, output: en.mp4}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_render_set(manifest)


@pytest.mark.parametrize("duplicate", ["scenario", "output", "provider"])
def test_load_render_set_rejects_cross_variant_collisions(tmp_path: Path, duplicate: str) -> None:
    first = tmp_path / "first.scenario.yaml"
    second = tmp_path / "second.scenario.yaml"
    first.write_text(_scenario("pl-PL", "pol"), encoding="utf-8")
    second.write_text(
        _scenario("en-US", provider="other" if duplicate == "provider" else "fake"),
        encoding="utf-8",
    )
    second_source = "first.scenario.yaml" if duplicate == "scenario" else "second.scenario.yaml"
    second_output = "pl.mp4" if duplicate == "output" else "en.mp4"
    manifest = _write_set(
        tmp_path,
        f"""\
              pl-PL: {{scenario: first.scenario.yaml, output: pl.mp4}}
              en-US: {{scenario: {second_source}, output: {second_output}}}
        """,
    )

    expected = {
        "scenario": "więcej niż raz",
        "output": "koliduje",
        "provider": "jednego providera",
    }[duplicate]
    with pytest.raises(RenderSetValidationError, match=expected):
        load_render_set(manifest)


def test_load_render_set_rejects_compiled_sidecar_collision(tmp_path: Path) -> None:
    (tmp_path / "same.scenario.yaml").write_text(_scenario("pl-PL", "pol"), encoding="utf-8")
    (tmp_path / "same.scenario.yml").write_text(_scenario("en-US"), encoding="utf-8")
    manifest = _write_set(
        tmp_path,
        """\
              pl-PL: {scenario: same.scenario.yaml, output: pl.mp4}
              en-US: {scenario: same.scenario.yml, output: en.mp4}
        """,
    )

    with pytest.raises(RenderSetValidationError, match="sidecar kompilacji"):
        load_render_set(manifest)


def test_load_render_set_rejects_sidecar_that_would_overwrite_manifest(tmp_path: Path) -> None:
    (tmp_path / "job.scenario.yaml").write_text(_scenario("en-US"), encoding="utf-8")
    manifest = tmp_path / "job.compiled.yaml"
    manifest.write_text(
        "kind: localized-render-set\n"
        "version: 1\n"
        "variants:\n"
        "  en-US: {scenario: job.scenario.yaml, output: en.mp4}\n",
        encoding="utf-8",
    )

    with pytest.raises(RenderSetValidationError, match="nadpisałby manifest"):
        load_render_set(manifest)


def test_load_render_set_rejects_scenario_symlink_outside_manifest_directory(
    tmp_path: Path,
) -> None:
    manifest_dir = tmp_path / "set"
    manifest_dir.mkdir()
    outside = tmp_path / "outside.scenario.yaml"
    outside.write_text(_scenario("en-US"), encoding="utf-8")
    (manifest_dir / "alias.scenario.yaml").symlink_to(outside)
    manifest = _write_set(
        manifest_dir,
        "      en-US: {scenario: alias.scenario.yaml, output: en.mp4}",
    )

    with pytest.raises(RenderSetValidationError, match="linków symbolicznych"):
        load_render_set(manifest)
