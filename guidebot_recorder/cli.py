"""CLI: `guidebot compile / render / validate`."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import typer
from playwright.async_api import async_playwright
from pydantic import ValidationError

from guidebot_recorder.recorder.compile import compile_up_to_date, run_compile_in_browser
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.recorder.render_set import (
    RenderSetError,
    ensure_render_set_compiled,
    render_set_output_paths,
    render_set_up_to_date,
    run_compile_set,
    run_render_set,
)
from guidebot_recorder.resolver.reasoner import CodexReasoner
from guidebot_recorder.scenario.loader import (
    CompiledSidecarError,
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


def _load_set_or_exit(path: Path) -> RenderSetPlan:
    try:
        return load_render_set(path)
    except Exception as exc:  # noqa: BLE001 — CLI reports the full manifest/scenario error
        typer.echo(f"BŁĄD zestawu: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command("validate")
def validate_cmd(path: Path) -> None:
    """Wczytaj i zwaliduj schemat scenariusza (bez przeglądarki)."""
    try:
        load_scenario(path)
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
    if not force and compile_up_to_date(path):
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
        try:
            if render_set_up_to_date(plan):
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

    try:
        asyncio.run(_run())
    except RenderSetError as exc:
        typer.echo(f"BŁĄD: {exc}", err=True)
        raise typer.Exit(code=1) from None


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

    try:
        outputs = asyncio.run(_run())
    except RenderSetError as exc:
        typer.echo(f"BŁĄD: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for output in outputs:
        typer.echo(f"zrenderowano: {output}")


if __name__ == "__main__":
    app()
