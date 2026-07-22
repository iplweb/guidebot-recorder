"""Taking the still images a guide page is built around.

Two ways to photograph a step, and they differ in *when*, not in how. Most steps
are photographed by :func:`_screenshot` at a moment the capture loop picks —
before a click, after a fill. A ``select:`` step cannot be: the only instant
worth documenting is inside the interaction, so :class:`_OpenListFrame` is handed
to the recorder and fires from there.

Named "stills" and not "frames" on purpose: in this codebase ``frame`` means a
Playwright frame everywhere else.

Split out of :mod:`~guidebot_recorder.guide.capture` so that module can stay one
file: nothing here is a test seam, and nothing here knows about the capture loop.
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Page

from guidebot_recorder.recorder.recorder import SelectReveal


async def _screenshot(page: Page, shots_dir: Path, index: int) -> tuple[Path, tuple[int, int]]:
    shots_dir.mkdir(parents=True, exist_ok=True)
    path = shots_dir / f"step-{index:03d}.png"
    await page.screenshot(path=str(path))
    size = page.viewport_size or {"width": 1280, "height": 720}
    return path, (size["width"], size["height"])


class _OpenListFrame:
    """The `select:` step's screenshot, taken while its option list is unfurled.

    `select:` is the one action whose frame belongs *inside* the interaction
    rather than before or after it. `click`/`hover` are photographed before the
    action and `type` after the fill, but a dropdown that is worth documenting is
    only itself for one instant: list open, option row under the cursor, nothing
    chosen yet. A frame taken after the choice shows a collapsed control that has
    silently changed value — which is the complaint this whole capture exists to
    answer.

    `Recorder.select` awaits this at exactly that instant, on every class of
    control, so everything the PDF page needs is read here: none of it survives
    the click, and the box the cursor approach measured beforehand is by then the
    *collapsed* control's, which for a page-enhanced select was never even on
    screen.
    """

    def __init__(self, page: Page, shots_dir: Path, index: int) -> None:
        self._page = page
        self._shots_dir = shots_dir
        self._index = index
        self.shot: Path | None = None
        self.size: tuple[int, int] | None = None
        self.reveal: SelectReveal | None = None

    async def __call__(self, reveal: SelectReveal) -> None:
        self.reveal = reveal
        self.shot, self.size = await _screenshot(self._page, self._shots_dir, self._index)
