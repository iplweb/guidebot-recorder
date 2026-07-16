"""Build one language-independent SFX bed (bounded 3-input ffmpeg graph)."""

from __future__ import annotations

from pathlib import Path

from guidebot_recorder.video.mux import SAMPLE_RATE, _run_to_output, ffmpeg_bin


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
    sources = [(path, offs) for path, offs in by_kind.values() if offs]
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
    for path, _ in sources:
        cmd += ["-i", str(path)]

    filters: list[str] = []
    mix_labels = ["[0:a]"]
    for idx, (_path, offs) in enumerate(sources, start=1):
        base = f"[{idx}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo"
        if len(offs) == 1:
            filters.append(f"{base},adelay={int(round(offs[0] * 1000))}:all=1[s{idx}_0]")
            mix_labels.append(f"[s{idx}_0]")
        else:
            splits = "".join(f"[s{idx}_{j}]" for j in range(len(offs)))
            filters.append(f"{base},asplit={len(offs)}{splits}")
            for j, off in enumerate(offs):
                filters.append(f"[s{idx}_{j}]adelay={int(round(off * 1000))}:all=1[d{idx}_{j}]")
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
