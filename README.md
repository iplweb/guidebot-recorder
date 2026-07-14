# guidebot-recorder

[![CI](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

Kompiluj tekstowy scenariusz YAML do **deterministycznego filmu szkoleniowego**:
bot wchodzi na stronę, przechodzi daną funkcję krok po kroku (Playwright),
pokazuje kursor i kliknięcia, a lektor (TTS) tłumaczy, co się dzieje. Wynik to
plik `.mp4` z narracją.

## Jak to działa — kompilator dwufazowy

Scenariusz piszesz **intencjami po ludzku** („kliknij Zaloguj”). Osobna faza
**kompilacji** zamienia je na zamrożone, konkretne namiary na elementy, wpisując
je w ten sam plik. Dzięki temu właściwe **renderowanie jest deterministyczne** i
nie wymaga LLM-a — przeglądarka przechodzi całą funkcję jednym ciągiem, tak samo
przy każdym uruchomieniu.

```
scenario.yaml (intencje) ──compile (AI)──▶ scenario.yaml (+ zamrożone akcje) ──render (0×LLM)──▶ film.mp4
```

- **`compile`** — jedyna faza z AI. Resolver (domyślnie przez [Codex CLI]) mapuje
  instrukcję na semantyczny locator Playwrighta, waliduje unikalność i zamraża
  tożsamość elementu. Uruchamiany raz (i ponownie tylko przy zmianie strony).
- **`render`** — bez LLM. Czyta zamrożone akcje, animuje sztuczny kursor (ruch,
  „ripple”, highlight), nagrywa wideo, a narrację TTS miksuje do finalnego `.mp4`.

## Instalacja

Wymaga **Python 3.12+**, [uv], **ffmpeg** oraz przeglądarki Chromium (Playwright).

```bash
uv sync
uv run playwright install chromium
# ffmpeg: macOS `brew install ffmpeg`, Debian/Ubuntu `apt install ffmpeg`
```

Faza `compile` używa domyślnie [Codex CLI] (`npm i -g @openai/codex`) — działa na
subskrypcji, bez klucza API. Resolver jest wymienny (interfejs `Reasoner`).

## Użycie

```bash
# 1. sprawdź schemat scenariusza
uv run guidebot validate examples/login.scenario.yaml

# 2. skompiluj intencje → zamrożone akcje (faza AI, wpisuje w ten sam plik)
uv run guidebot compile examples/login.scenario.yaml

# 3. zrenderuj deterministyczny film z lektorem
uv run guidebot render examples/login.scenario.yaml --out out/login.mp4
```

### Scenariusz

```yaml
config:
  title: "Logowanie do systemu"
  baseUrl: https://example.com
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts: { provider: edge, voice: pl-PL-MarekNeural, lang: pl-PL }
steps:
  - say: "Witaj. Zaraz pokażę, jak zalogować się do systemu."
  - navigate: /login
  - teach: "Aby się zalogować, należy kliknąć przycisk Zaloguj w prawym górnym rogu"
  - enterText: { into: "pole adresu e-mail", text: "${DEMO_EMAIL}" }
    say: "Teraz wpisuję swój adres e-mail."
```

Komendy: `say` (sama narracja), `teach` (lektor mówi całe zdanie-przewodnik, a bot
wykonuje wyłuskaną z niego akcję), `enterText`, `navigate`, `wait` (czas lub
warunek na elemencie), oraz `click`/`hover` jako jawne escape-hatche. Wartości
sekretów podstawiaj przez `${ENV_VAR}` — nie trafiają do repo.

## Status

Wczesna wersja (beta). Ścieżki AI (`compile` przez Codex) i realny głos (edge-tts)
są zaimplementowane i testowane jednostkowo; pełny pakiet testów obejmuje
deterministyczny render end-to-end (Playwright + ffmpeg) z zamockowanym resolverem
i cichym TTS.

## Licencja

[MIT](LICENSE) © 2026 Michał Pasternak

[uv]: https://docs.astral.sh/uv/
[Codex CLI]: https://github.com/openai/codex
