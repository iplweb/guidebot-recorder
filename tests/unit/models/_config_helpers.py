"""Wspólny budowniczy `Config` dla plików `test_config_*.py`.

Powstał przy podziale `test_config.py` (971 linii) na pięć plików tematycznych.
`_cfg()` jest używany przez wszystkie pięć — trzyma minimalny, poprawny `Config`
w jednym miejscu, więc dodanie do modelu kolejnego pola wymaganego to jedna
poprawka, nie pięć.

Świadomie NIE jest to `conftest.py` (decyzja D2 z
`docs/superpowers/specs/2026-07-22-code-cleanup-design.md`): pomocnik trzeba
zaimportować jawnie, żeby czytając plik testowy widzieć, skąd bierze się każda
nazwa.
"""

from guidebot_recorder.models.config import (
    ChromeConfig,
    Config,
    PopupConfig,
    TtsConfig,
    Viewport,
)


def _cfg(
    w=1280,
    locale="pl-PL",
    chrome: ChromeConfig | None = None,
    popup: PopupConfig | None = None,
):
    return Config(
        title="t",
        viewport=Viewport(width=w, height=720),
        locale=locale,
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
        **({"chrome": chrome} if chrome is not None else {}),
        **({"popup": popup} if popup is not None else {}),
    )
