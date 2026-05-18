from __future__ import annotations

import re
from pathlib import Path


BURSTS_ROOT = Path("data/bursts")
OUTPUT_PATH = Path("configs/prompts/tts_bursts.md")


def filename_to_burst_token(wav_path: Path) -> str:
    """
    Convert local WAV filenames to TTS burst tokens.

    Example:
        hard_laugh-01.wav -> hard_laugh_1
        yawn-03.wav       -> yawn_3

    This follows the API example:
        <burst(yawn_1)>
    """

    stem = wav_path.stem

    match = re.match(r"(.+)-0*([0-9]+)$", stem)

    if match:
        name = match.group(1)
        number = match.group(2)
        return f"{name}_{number}"

    return stem.replace("-", "_")


def main() -> None:
    if not BURSTS_ROOT.exists():
        raise FileNotFoundError(f"Bursts folder not found: {BURSTS_ROOT}")

    groups: dict[str, list[str]] = {}

    for wav_path in sorted(BURSTS_ROOT.rglob("*.wav")):
        category = wav_path.parent.name
        token = filename_to_burst_token(wav_path)

        groups.setdefault(category, []).append(token)

    lines: list[str] = []

    lines.append("# Available TTS Burst Annotations")
    lines.append("")
    lines.append("The assistant may use the following TTS burst annotations.")
    lines.append("")
    lines.append("Use this exact syntax in the annotated response:")
    lines.append("")
    lines.append("[burst: burst_name]")
    lines.append("")
    lines.append("Examples:")
    lines.append("")
    lines.append("[burst: yawn_1]")
    lines.append("[burst: thinking_1]")
    lines.append("[burst: hard_laugh_1]")
    lines.append("")
    lines.append("Do not invent burst names.")
    lines.append("Only use burst names listed below.")
    lines.append("Use bursts sparingly and only when they sound natural.")
    lines.append("Do not use more than one burst in a short response unless strongly justified.")
    lines.append("")

    for category, tokens in sorted(groups.items()):
        lines.append(f"## {category}")
        lines.append("")

        for token in tokens:
            lines.append(f"- {token}")

        lines.append("")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")

    total = sum(len(tokens) for tokens in groups.values())

    print(f"Generated: {OUTPUT_PATH}")
    print(f"Categories: {len(groups)}")
    print(f"Bursts: {total}")


if __name__ == "__main__":
    main()