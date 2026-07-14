"""Golden-diff: wstrzyknięcie cachedAction zachowuje komentarze/kolejność +
zapis atomowy (Task 8, §4)."""

import io

from ruamel.yaml import YAML

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.scenario.roundtrip import atomic_write, inject_cached_action

RAW = """\
config:
  title: t                 # tytuł szkolenia
  viewport: { width: 1, height: 1 }
  tts: { provider: e, voice: v, lang: pl }
steps:
  - teach: kliknij X       # ważny komentarz
  - say: koniec
"""


def _load_doc(path):
    return YAML(typ="rt").load(path.read_text(encoding="utf-8"))


def _dump(doc):
    buf = io.StringIO()
    YAML(typ="rt").dump(doc, buf)
    return buf.getvalue()


def _action():
    return CachedAction(
        action="click",
        target=RoleTarget(role="button", name="Zaloguj"),
        identity=Identity(tag="button", ancestry_digest="d1"),
        expect="navigation",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="kliknij X",
            expect="navigation",
            compiler_version=1,
            config_hash="c19a",
        ),
    )


def test_injection_adds_cached_action(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text(RAW, encoding="utf-8")
    doc = _load_doc(src)

    inject_cached_action(doc, 0, _action())

    step0 = doc["steps"][0]
    assert "cachedAction" in step0
    ca = step0["cachedAction"]
    assert ca["action"] == "click"
    assert ca["target"]["strategy"] == "role"
    assert ca["target"]["name"] == "Zaloguj"
    assert ca["identity"]["tag"] == "button"
    assert ca["fingerprint"]["config_hash"] == "c19a"


def test_injection_excludes_none(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text(RAW, encoding="utf-8")
    doc = _load_doc(src)

    inject_cached_action(doc, 0, _action())

    ca = doc["steps"][0]["cachedAction"]
    # identity.testid=None, state=None → pominięte przez exclude_none
    assert "testid" not in ca["identity"]
    assert "state" not in ca


def test_injection_preserves_comments_and_order(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text(RAW, encoding="utf-8")
    doc = _load_doc(src)

    inject_cached_action(doc, 0, _action())
    out = _dump(doc)

    assert "# ważny komentarz" in out
    assert "# tytuł szkolenia" in out
    # kolejność kroków zachowana, drugi krok nietknięty
    assert out.index("teach: kliknij X") < out.index("say: koniec")


def test_atomic_write_roundtrips(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text(RAW, encoding="utf-8")
    doc = _load_doc(src)

    inject_cached_action(doc, 0, _action())
    atomic_write(src, doc)

    text = src.read_text(encoding="utf-8")
    assert "# ważny komentarz" in text
    assert "cachedAction:" in text
    # ponowne wczytanie po zapisie parsuje się i ma cachedAction
    reloaded = _load_doc(src)
    assert reloaded["steps"][0]["cachedAction"]["action"] == "click"


def test_atomic_write_no_temp_left(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text(RAW, encoding="utf-8")
    doc = _load_doc(src)
    inject_cached_action(doc, 0, _action())
    atomic_write(src, doc)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "s.yaml"]
    assert leftovers == [], f"pozostały pliki tymczasowe: {leftovers}"
