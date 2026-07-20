"""Build one language-independent SFX bed (bounded 3-input ffmpeg graph)."""

from __future__ import annotations

from pathlib import Path

from guidebot_recorder.video.mux import SAMPLE_RATE, _run_to_output, ffmpeg_bin

# The assets retain ample source headroom, then are balanced here before the
# scenario-wide ``sound.volume`` attenuation.  A mouse click is sparse and must cut
# through speech; keys repeat rapidly and therefore stay softer.  Small pitch/level
# changes stop a typed word from sounding like one perfectly looped sample.
_KIND_GAIN_DB = {"click": 18.0, "key": 12.0}
_KEY_VARIANTS = (
    (1.000, 0.0),
    (1.035, -0.6),
    (0.972, -1.0),
    (1.060, -1.4),
    (0.945, -0.7),
)


def _event_filters(kind: str, event_index: int) -> str:
    """Return deterministic per-event colour without moving its onset time."""

    if kind != "key":
        return ""
    pitch, gain_db = _KEY_VARIANTS[event_index % len(_KEY_VARIANTS)]
    rate = int(round(SAMPLE_RATE * pitch))
    return f"asetrate={rate},aresample={SAMPLE_RATE},volume={gain_db}dB,"


def build_sfx_bed(
    events: list[tuple[str, float]],
    total: float,
    out: Path,
    *,
    click_path: Path,
    key_path: Path,
    gain_db: float,
) -> None:
    """Render click/key sound effects to *out*, exactly *total* seconds long.

    Each event is delayed to its offset and mixed over a silent base spanning
    the whole timeline, then gained by *gain_db*. The ffmpeg input count is
    bounded to at most 3 (silence + click + key): a source kind with zero
    events is omitted entirely rather than fed an unconnected pad.
    """
    out = Path(out)
    for _kind, offset in events:
        if offset < 0:
            raise ValueError(f"sfx offset must be >= 0, got {offset}")

    by_kind = {"click": (Path(click_path), []), "key": (Path(key_path), [])}
    for kind, offset in events:
        if kind in by_kind:
            by_kind[kind][1].append(offset)
    sources = [(kind, path, offs) for kind, (path, offs) in by_kind.items() if offs]
    if not sources:
        return  # no events → no bed

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{total:.6f}",
        "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
    ]
    for _kind, path, _offs in sources:
        cmd += ["-i", str(path)]

    filters: list[str] = []
    mix_labels = ["[0:a]"]
    for idx, (kind, _path, offs) in enumerate(sources, start=1):
        kind_gain = _KIND_GAIN_DB[kind]
        base = (
            f"[{idx}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,volume={kind_gain}dB"
        )
        if len(offs) == 1:
            colour = _event_filters(kind, 0)
            filters.append(f"{base},{colour}adelay={int(round(offs[0] * 1000))}:all=1[s{idx}_0]")
            mix_labels.append(f"[s{idx}_0]")
        else:
            splits = "".join(f"[s{idx}_{j}]" for j in range(len(offs)))
            filters.append(f"{base},asplit={len(offs)}{splits}")
            for j, off in enumerate(offs):
                colour = _event_filters(kind, j)
                filters.append(
                    f"[s{idx}_{j}]{colour}adelay={int(round(off * 1000))}:all=1[d{idx}_{j}]"
                )
                mix_labels.append(f"[d{idx}_{j}]")

    filters.append(
        f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=longest:normalize=0[m]"
    )
    filters.append(f"[m]volume={gain_db}dB[out]")

    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-ar",
        str(SAMPLE_RATE),
        "-t",
        f"{total:.6f}",
    ]
    _run_to_output(cmd, out)


def mix_sfx_into_bed(narration_bed: Path, sfx_bed: Path, out: Path, total: float) -> None:
    """Mix the shared (already gain-applied) SFX bed under a narration bed.

    Two-input amix (normalize=0 so neither is auto-scaled) plus a hard limiter with
    level=disabled (alimiter's `level` defaults to true and would re-normalize the
    output back toward full scale, defeating the ceiling); re-trimmed to exactly
    `total` so `mux_audio_tracks`' 0.05 tolerance holds. narration_bed and out MUST
    be different paths (no in-place).
    """
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(narration_bed),
        "-i",
        str(sfx_bed),
        "-filter_complex",
        "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[m];"
        "[m]alimiter=limit=0.95:level=disabled[out]",
        "-map",
        "[out]",
        "-ar",
        str(SAMPLE_RATE),
        "-t",
        f"{total:.6f}",
    ]
    _run_to_output(cmd, out)
