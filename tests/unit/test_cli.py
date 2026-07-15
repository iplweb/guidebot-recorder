import textwrap

from typer.testing import CliRunner

from guidebot_recorder.cli import app

runner = CliRunner()

GOOD = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 640, height: 480}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - say: "Witaj"
    """
)

BAD_TWO_COMMANDS = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: e, voice: v, lang: pl}
    steps:
      - click: "X"
        navigate: "http://x"
    """
)


def test_validate_ok(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(GOOD, encoding="utf-8")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_validate_rejects_two_commands(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(BAD_TWO_COMMANDS, encoding="utf-8")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code != 0


def test_render_auto_heal_not_implemented(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(GOOD, encoding="utf-8")
    result = runner.invoke(
        app, ["render", str(path), "--out", str(tmp_path / "o.mp4"), "--auto-heal"]
    )
    assert result.exit_code != 0


def test_render_rejects_non_edge_provider_before_browser_launch(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(GOOD.replace("provider: edge", "provider: custom"), encoding="utf-8")

    result = runner.invoke(app, ["render", str(path), "--out", str(tmp_path / "o.mp4")])

    assert result.exit_code == 2
    assert "obsługuje provider TTS `edge`" in result.output
