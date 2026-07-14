"""CLI: `guidebot compile / render / validate`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.reasoner import CodexReasoner
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.tts.edge import EdgeTtsProvider

app = typer.Typer(help="Kompilator scenariuszy YAML → deterministyczny film szkoleniowy.")


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
def compile_cmd(path: Path) -> None:
    """Skompiluj intencje → `cachedAction` (in-place, faza AI)."""

    async def _run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            try:
                await run_compile(path, page, CodexReasoner())
            finally:
                await browser.close()

    asyncio.run(_run())
    typer.echo("skompilowano")


@app.command("render")
def render_cmd(
    path: Path,
    out: Path = typer.Option(..., "--out", "-o", help="Ścieżka wyjściowa .mp4"),
    auto_heal: bool = typer.Option(False, "--auto-heal", help="(niezaimplementowane w v1)"),
) -> None:
    """Zrenderuj deterministyczny film `.mp4` z lektorem (0×LLM)."""
    if auto_heal:
        typer.echo("BŁĄD: --auto-heal nie jest zaimplementowane w v1", err=True)
        raise typer.Exit(code=2)

    async def _run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                await run_render(path, out, EdgeTtsProvider(), Path(".guidebot/audio"), browser)
            finally:
                await browser.close()

    asyncio.run(_run())
    typer.echo(f"zrenderowano: {out}")


if __name__ == "__main__":
    app()
