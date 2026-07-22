"""CLI: `guidebot compile / render / validate`."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from playwright.async_api import async_playwright
from pydantic import ValidationError

from guidebot_recorder.recorder.compile import (
    compile_up_to_date,
    needs_positional_recheck,
    run_compile_in_browser,
)
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.recorder.render_set import (
    RenderSetError,
    ensure_render_set_compiled,
    render_set_needs_positional_recheck,
    render_set_output_paths,
    render_set_up_to_date,
    run_compile_set,
    run_render_set,
)
from guidebot_recorder.recorder.session import (
    SetupNeedsCompile,
    SetupSessionError,
    establish_session,
)
from guidebot_recorder.resolver.reasoner import CodexReasoner
from guidebot_recorder.scenario.loader import (
    CompiledSidecarError,
    ScenarioValidationError,
    guard_source_scenario,
    load_scenario,
)
from guidebot_recorder.scenario.render_set import RenderSetPlan, load_render_set
from guidebot_recorder.tts.edge import EdgeTtsProvider

app = typer.Typer(help="Kompilator scenariuszy YAML → deterministyczny film szkoleniowy.")


def _render_reasoner() -> CodexReasoner | None:
    """A Reasoner for render, or None when `codex` is not installed.

    `render` is LLM-free except for one case: an optional branch that was never
    compiled and does show up this time. Probing the binary is deliberate — the
    generic RuntimeError `CodexReasoner` raises is not something to string-match,
    and a missing binary must degrade to "skip the branch", not to a failed render.
    """

    return CodexReasoner() if shutil.which("codex") is not None else None


@contextmanager
def _scenario_errors(code: int) -> Iterator[None]:
    """Zgłoś `ScenarioValidationError` jako gotowy banner i wyjdź z kodem `code`.

    Wspólna dla wszystkich poleceń, bo scenariusz wczytuje się w każdym z nich —
    czasem dopiero po starcie przeglądarki (`guide`, `render`). Bez tego Typer
    renderował wyjątek jako panel tracebacku Richa, który łamie linie w środku
    ścieżek: `plik:linia` przestawał być kopiowalny do edytora.

    Banner idzie na stderr **dosłownie**: niesie własny nagłówek
    `BŁĄD walidacji — plik:linia` i wielolinijkowy fragment YAML, więc doklejenie
    prefiksu dałoby `BŁĄD walidacji: BŁĄD walidacji — …`, a `typer.echo` (czyli
    `click.echo`) zapisuje tekst bez zawijania i bez znaczników Richa.
    """

    try:
        yield
    except ScenarioValidationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=code) from None


def _load_set_or_exit(path: Path) -> RenderSetPlan:
    with _scenario_errors(code=1):
        try:
            return load_render_set(path)
        except ScenarioValidationError:
            raise  # gotowy banner — puszcza go `_scenario_errors`, bez prefiksu
        except Exception as exc:  # noqa: BLE001 — CLI reports the full manifest/scenario error
            typer.echo(f"BŁĄD zestawu: {exc}", err=True)
            raise typer.Exit(code=1) from None


@app.command("validate")
def validate_cmd(path: Path) -> None:
    """Wczytaj i zwaliduj schemat scenariusza (bez przeglądarki)."""
    with _scenario_errors(code=1):
        try:
            load_scenario(path)
        except ScenarioValidationError:
            raise  # gotowy banner — patrz `_scenario_errors`
        except Exception as exc:  # noqa: BLE001 — CLI: raportujemy każdy błąd walidacji
            typer.echo(f"BŁĄD walidacji: {exc}", err=True)
            raise typer.Exit(code=1) from None
    typer.echo("OK")


@app.command("compile")
def compile_cmd(
    path: Path,
    headed: bool = typer.Option(False, "--headed", help="Pokaż okno przeglądarki"),
    force: bool = typer.Option(False, "--force", help="Przelicz wszystkie kroki, ignoruj cache"),
    pause_on_error: bool = typer.Option(
        False, "--pause-on-error", help="Przy błędzie zatrzymaj i zostaw okno otwarte (headed)"
    ),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji Playwrighta (sekundy)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Pokaż postęp i kolejne kroki"),
) -> None:
    """Skompiluj intencje → `*.compiled.yaml` (faza AI)."""
    try:
        guard_source_scenario(path)
    except CompiledSidecarError as exc:
        typer.echo(f"BŁĄD: {exc}", err=True)
        raise typer.Exit(code=2) from None
    # kod 2 jak sąsiednie odrzucenie wejścia (`CompiledSidecarError`) wyżej
    with _scenario_errors(code=2):
        if not force and compile_up_to_date(path) and not needs_positional_recheck(path):
            typer.echo("nic do skompilowania (aktualne)")
            return

    async def _run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                await run_compile_in_browser(
                    path,
                    browser,
                    CodexReasoner(),
                    timeout=timeout,
                    force=force,
                    pause_on_error=pause_on_error,
                    verbose=verbose,
                )
            finally:
                await browser.close()

    # `run_compile_in_browser` wczytuje scenariusz ponownie — po starcie przeglądarki
    with _scenario_errors(code=2):
        asyncio.run(_run())
    typer.echo("skompilowano")


@app.command("compile-set")
def compile_set_cmd(
    path: Path,
    headed: bool = typer.Option(False, "--headed", help="Pokaż okno przeglądarki"),
    force: bool = typer.Option(False, "--force", help="Przelicz wszystkie warianty"),
    pause_on_error: bool = typer.Option(
        False, "--pause-on-error", help="Przy błędzie zostaw okno otwarte (headed)"
    ),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji (sekundy)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Pokaż postęp"),
) -> None:
    """Skompiluj osobny sidecar dla każdego scenariusza językowego."""

    plan = _load_set_or_exit(path)
    if not force:
        # kod 1 jak reszta obsługi błędów zestawu w tym poleceniu
        with _scenario_errors(code=1):
            try:
                # Bramka kompilacji, więc pyta o obie rzeczy: czy sidecary
                # odpowiadają źródłom, ORAZ czy któryś nie niesie namiaru
                # pozycyjnego, którego dryf da się sprawdzić tylko w otwartej
                # przeglądarce. Bez tego drugiego pytania `compile-set` kończy
                # pracę tutaj i wykrywanie dryfu jest martwe dla zestawów.
                if render_set_up_to_date(plan) and not render_set_needs_positional_recheck(plan):
                    typer.echo("nic do skompilowania (wszystkie warianty aktualne)")
                    return
            except RenderSetError as exc:
                typer.echo(f"BŁĄD: {exc}", err=True)
                raise typer.Exit(code=1) from None

    async def _run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                result = await run_compile_set(
                    plan,
                    browser,
                    CodexReasoner(),
                    timeout=timeout,
                    force=force,
                    pause_on_error=pause_on_error,
                    verbose=verbose,
                )
            finally:
                await browser.close()
        typer.echo(
            "skompilowano warianty: " + (", ".join(result.compiled) if result.compiled else "brak")
        )

    with _scenario_errors(code=1):
        try:
            asyncio.run(_run())
        except RenderSetError as exc:
            typer.echo(f"BŁĄD: {exc}", err=True)
            raise typer.Exit(code=1) from None


@app.command("setup")
def setup_cmd(
    scenario: Path = typer.Argument(..., help="Scenariusz setup (przygotowanie sesji)"),
    headed: bool = typer.Option(
        False, "--headed", help="Pokaż okno; pozwól dokończyć logowanie ręcznie (MFA/captcha)"
    ),
    force: bool = typer.Option(False, "--force", help="Zawsze odbuduj sesję, ignoruj cache"),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji Playwrighta (sekundy)"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Pokaż postęp"),
) -> None:
    """Przygotuj i zbuforuj sesję (logowanie itp.) z scenariusza setup."""

    async def _run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                status, _state = await establish_session(
                    browser,
                    scenario,
                    Path(".guidebot/sessions"),
                    None,
                    timeout=timeout,
                    force=force,
                    manual=headed,
                )
            finally:
                await browser.close()
        if status == "reused":
            typer.echo("session reused (already live)")
        else:
            typer.echo("session refreshed and cached")

    # `establish_session` też wczytuje scenariusz — po starcie przeglądarki
    with _scenario_errors(code=1):
        try:
            asyncio.run(_run())
        except (SetupNeedsCompile, SetupSessionError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None


@app.command("render")
def render_cmd(
    path: Path,
    out: Path = typer.Option(..., "--out", "-o", help="Ścieżka wyjściowa .mp4"),
    headed: bool = typer.Option(False, "--headed", help="Pokaż okno przeglądarki"),
    pause_on_error: bool = typer.Option(
        False, "--pause-on-error", help="Przy błędzie zatrzymaj i zostaw okno otwarte (headed)"
    ),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji Playwrighta (sekundy)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Pokaż postęp i kolejne kroki"),
    auto_heal: bool = typer.Option(False, "--auto-heal", help="(niezaimplementowane w v1)"),
    hold_frame: bool | None = typer.Option(
        None,
        "--hold-frame/--no-hold-frame",
        help=(
            "Zamroź klatkę na czas narracji zamiast czekać w czasie rzeczywistym "
            "(domyślnie z konfiguracji)."
        ),
    ),
    hold_frame_settle: float | None = typer.Option(
        None,
        "--hold-frame-settle",
        help="Sekundy realnego czasu przed zamrożeniem klatki (domyślnie z konfiguracji).",
    ),
    dump_timeline: bool = typer.Option(
        False,
        "--dump-timeline",
        help="Zapisz wyliczoną oś czasu obok pliku wideo (diagnostyka).",
    ),
) -> None:
    """Zrenderuj `.mp4` z jedną lub wieloma ścieżkami lektora (0×LLM)."""
    if auto_heal:
        typer.echo("BŁĄD: --auto-heal nie jest zaimplementowane w v1", err=True)
        raise typer.Exit(code=2)

    try:
        guard_source_scenario(path)
    except CompiledSidecarError as exc:
        typer.echo(f"BŁĄD: {exc}", err=True)
        raise typer.Exit(code=2) from None

    # kod 2 jak sąsiednie odrzucenia wejścia w tym poleceniu
    with _scenario_errors(code=2):
        scenario = load_scenario(path)
    cfg = scenario.config
    # `run_render` reloads the scenario from `path` itself, so the hold-frame
    # flags are passed to it as explicit overrides rather than mutated onto this
    # Config (which the renderer never sees). `None` keeps the scenario's value.
    providers = {track.provider for track in [cfg.tts, *cfg.audio_tracks]}
    if providers != {"edge"}:
        configured = ", ".join(sorted(providers))
        typer.echo(
            "BŁĄD: wbudowane polecenie `render` obsługuje provider TTS `edge`; "
            f"skonfigurowano: {configured}",
            err=True,
        )
        raise typer.Exit(code=2)

    # `run_render` applies this override by ASSIGNING onto its own reloaded
    # Config (see the comment above), which is exactly the same
    # `validate_assignment` path exercised here — so a value the field
    # rejects (e.g. `--hold-frame-settle 0`) is caught NOW, before the browser
    # ever launches, instead of surfacing as an unhandled ValidationError deep
    # inside `_run()` after Chromium is already up.
    if hold_frame_settle is not None:
        try:
            cfg.hold_frame_settle = hold_frame_settle
        except ValidationError as exc:
            typer.echo(f"BŁĄD: nieprawidłowa wartość --hold-frame-settle: {exc}", err=True)
            raise typer.Exit(code=2) from None

    async def _run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                await run_render(
                    path,
                    out,
                    EdgeTtsProvider(),
                    Path(".guidebot/audio"),
                    browser,
                    timeout=timeout,
                    pause_on_error=pause_on_error,
                    verbose=verbose,
                    hold_frame=hold_frame,
                    hold_frame_settle=hold_frame_settle,
                    dump_timeline=dump_timeline,
                    reasoner=_render_reasoner(),
                )
            finally:
                await browser.close()

    # `run_render` wczytuje scenariusz ponownie — już po starcie przeglądarki
    with _scenario_errors(code=2):
        asyncio.run(_run())
    typer.echo(f"zrenderowano: {out}")


@app.command("render-set")
def render_set_cmd(
    path: Path,
    out_dir: Path = typer.Option(..., "--output-dir", "--out-dir", help="Katalog wynikowych MP4"),
    headed: bool = typer.Option(False, "--headed", help="Pokaż okno przeglądarki"),
    pause_on_error: bool = typer.Option(
        False, "--pause-on-error", help="Przy błędzie zostaw okno otwarte (headed)"
    ),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji (sekundy)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Pokaż postęp"),
) -> None:
    """Wyrenderuj osobny zlokalizowany MP4 z jednym audio na wariant."""

    plan = _load_set_or_exit(path)
    if plan.provider != "edge":
        typer.echo(
            "BŁĄD: wbudowane `render-set` obsługuje provider TTS `edge`; "
            f"skonfigurowano: {plan.provider}",
            err=True,
        )
        raise typer.Exit(code=2)
    with _scenario_errors(code=1):  # kod 1 jak reszta obsługi błędów zestawu
        try:
            render_set_output_paths(plan, out_dir)
            ensure_render_set_compiled(plan)
        except RenderSetError as exc:
            typer.echo(f"BŁĄD: {exc}", err=True)
            raise typer.Exit(code=2) from None

    async def _run() -> list[Path]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                return await run_render_set(
                    plan,
                    out_dir,
                    EdgeTtsProvider(),
                    Path(".guidebot/audio"),
                    browser,
                    timeout=timeout,
                    pause_on_error=pause_on_error,
                    verbose=verbose,
                    reasoner=_render_reasoner(),
                )
            finally:
                await browser.close()

    with _scenario_errors(code=1):
        try:
            outputs = asyncio.run(_run())
        except RenderSetError as exc:
            typer.echo(f"BŁĄD: {exc}", err=True)
            raise typer.Exit(code=1) from None
    for output in outputs:
        typer.echo(f"zrenderowano: {output}")


@app.command("guide")
def guide_cmd(
    path: Path,
    out: Path = typer.Option(..., "--out", "-o", help="Ścieżka wyjściowa .pdf"),
    headed: bool = typer.Option(False, "--headed", help="Pokaż okno przeglądarki"),
    pause_on_error: bool = typer.Option(
        False, "--pause-on-error", help="Przy błędzie zatrzymaj i zostaw okno otwarte (headed)"
    ),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji Playwrighta (sekundy)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Pokaż postęp"),
) -> None:
    """Zbuduj przewodnik PDF krok-po-kroku ze skompilowanego scenariusza (0×LLM)."""
    from guidebot_recorder.guide.guide import run_guide
    from guidebot_recorder.guide.prolog import GuideError

    async def _run() -> int:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                return await run_guide(
                    path,
                    out,
                    browser,
                    timeout=timeout,
                    verbose=verbose,
                    pause_on_error=pause_on_error,
                )
            finally:
                await browser.close()

    # `run_guide` wczytuje scenariusz po starcie przeglądarki; kod 2 jak `GuideError`
    with _scenario_errors(code=2):
        try:
            count = asyncio.run(_run())
        except GuideError as exc:
            typer.echo(f"BŁĄD: {exc}", err=True)
            raise typer.Exit(code=2) from None
    typer.echo(f"zbudowano przewodnik: {out} ({count} stron)")


if __name__ == "__main__":
    app()
