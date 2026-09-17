#!/usr/bin/env python3
"""Create one sequential Korean/Japanese lesson audio file for Elle.

Input JSON format:
{
  "items": [
    {"japanese": "注文をお願いします。", "japanese_tts": "ちゅうもんをおねがいします。", "korean": "주문 부탁드립니다."}
  ]
}

When `japanese_tts` is present, it is used only for Japanese audio generation.
By default, the daily lesson generator now passes the original displayed
Japanese sentence through unchanged so kanji and katakana remain intact.

The output audio speaks, for each item in order:
  1. Korean meaning once with a Korean voice
  2. Japanese sentence three times with a Japanese voice

Default backend is local mlx-audio/Qwen3-TTS via ~/mlx-tts. The legacy
macOS `say` backend remains available with --tts-backend macos.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

DEFAULT_JA_VOICE = "Kyoko"
DEFAULT_KO_VOICE = "Yuna"
# Slightly slower than macOS defaults sounds more natural for language study.
DEFAULT_JA_RATE = "140"
DEFAULT_KO_RATE = "145"

DEFAULT_TTS_BACKEND = "mlx"
DEFAULT_MLX_PYTHON = str(Path.home() / "mlx-tts" / "bin" / "python")
DEFAULT_MLX_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
# Current Qwen3 CustomVoice snapshot supports: serena, vivian, uncle_fu, ryan,
# aiden, ono_anna, sohee, eric, dylan.
DEFAULT_MLX_KO_VOICE = "serena"
DEFAULT_MLX_JA_VOICE = "ono_anna"
DEFAULT_MLX_VOICE = None
DEFAULT_MLX_TEMPERATURE = 0.1
DEFAULT_MLX_MAX_TOKENS = 140
DEFAULT_MLX_REPETITION_PENALTY = 1.15
DEFAULT_MLX_INSTRUCT = (
    "Use Serena consistently for Korean meanings and Ono Anna consistently for Japanese sentences. "
    "Keep a calm, friendly teacher tone without changing speaker identity. "
    "Read exactly the provided text once, clearly, without adding or skipping words."
)
DEFAULT_BETWEEN_LANG_PAUSE = 2.0
DEFAULT_BETWEEN_ITEMS_PAUSE = 2.0
DEFAULT_JA_TEMPO = 1.0
MAX_TTS_ATTEMPTS = 2


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"required command not found: {name}")


def load_items(path: Path) -> list[dict[str, str]]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        die(f"failed to read input JSON {path}: {exc}")

    raw_items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        die("input JSON must be a list or an object with an 'items' list")

    items: list[dict[str, str]] = []
    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            die(f"item {idx} must be an object")
        japanese = str(raw.get("japanese") or raw.get("ja") or "").strip()
        japanese_tts = str(raw.get("japanese_tts") or raw.get("ja_tts") or raw.get("tts_japanese") or japanese).strip()
        korean = str(raw.get("korean") or raw.get("ko") or raw.get("meaning") or "").strip()
        if not japanese or not korean:
            die(f"item {idx} needs both japanese and korean text")
        if not japanese_tts:
            die(f"item {idx} needs japanese_tts text when japanese text is empty after normalization")
        item = {"japanese": japanese, "japanese_tts": japanese_tts, "korean": korean}
        sentence_id = str(raw.get("sentence_id") or raw.get("id") or "").strip()
        if sentence_id:
            item["sentence_id"] = sentence_id
        items.append(item)

    if not items:
        die("input contains no sentence items")
    if len(items) > 20:
        die("refusing to create audio for more than 20 items at once")
    return items


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_with_output(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as src:
        frames = src.getnframes()
        rate = src.getframerate()
    if rate <= 0:
        die(f"invalid wav sample rate for {path}")
    return frames / rate


def expected_duration_range(text: str, lang_code: str) -> tuple[float, float]:
    """Return a broad sanity range for one trimmed TTS segment.

    This is not speech recognition; it catches common local TTS failures such as
    near-silent/truncated output or runaway/repeated output before concatenation.
    """
    normalized = "".join(text.split())
    length = max(len(normalized), 1)
    if lang_code == "Japanese":
        return 0.8, max(3.5, min(12.0, 1.2 + length * 0.28))
    return 0.8, max(3.5, min(12.0, 1.2 + length * 0.22))


def validate_wav_segment(path: Path, text: str, lang_code: str) -> float:
    duration = wav_duration_seconds(path)
    min_seconds, max_seconds = expected_duration_range(text, lang_code)
    if duration < min_seconds:
        raise ValueError(
            f"{lang_code} TTS segment too short for {path}: "
            f"{duration:.2f}s < {min_seconds:.2f}s; text={text!r}"
        )
    if duration > max_seconds:
        raise ValueError(
            f"{lang_code} TTS segment too long for {path}: "
            f"{duration:.2f}s > {max_seconds:.2f}s; text={text!r}"
        )
    return duration


def soften_text(text: str) -> str:
    """Make short study sentences sound less clipped with macOS voices."""
    text = " ".join(text.strip().split())
    if not text:
        return text
    if text[-1] not in ".。!！?？…":
        text += "。"
    return text


def say_to_wav(text: str, voice: str, rate: str, wav_path: Path, tmp_dir: Path) -> None:
    aiff_path = tmp_dir / f"{wav_path.stem}.aiff"
    run(["say", "-v", voice, "-r", rate, "-o", str(aiff_path), soften_text(text)])
    run(["afconvert", "-f", "WAVE", "-d", "LEI16", str(aiff_path), str(wav_path)])


def mlx_to_wav(
    text: str,
    lang_code: str,
    wav_path: Path,
    tmp_dir: Path,
    *,
    mlx_python: str,
    mlx_model: str,
    mlx_voice: str,
    mlx_temperature: float | None,
    mlx_instruct: str,
    mlx_max_tokens: int | None,
    mlx_speed: float | None,
    mlx_duration_multiplier: float | None,
    mlx_repetition_penalty: float | None,
) -> None:
    out_dir = tmp_dir / f"mlx-{wav_path.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = wav_path.stem
    cmd = [
        mlx_python,
        "-m",
        "mlx_audio.tts.generate",
        "--model",
        mlx_model,
        "--text",
        soften_text(text),
        "--voice",
        mlx_voice,
        "--lang_code",
        lang_code,
        "--output_path",
        str(out_dir),
        "--file_prefix",
        prefix,
        "--audio_format",
        "wav",
    ]
    if mlx_instruct:
        cmd.extend(["--instruct", mlx_instruct])
    if mlx_temperature is not None:
        cmd.extend(["--temperature", str(mlx_temperature)])
    if mlx_max_tokens is not None:
        cmd.extend(["--max_tokens", str(mlx_max_tokens)])
    if mlx_speed is not None:
        cmd.extend(["--speed", str(mlx_speed)])
    if mlx_duration_multiplier is not None:
        cmd.extend(["--duration_multiplier", str(mlx_duration_multiplier)])
    if mlx_repetition_penalty is not None:
        cmd.extend(["--repetition_penalty", str(mlx_repetition_penalty)])
    last_error = ""
    for attempt in range(1, MAX_TTS_ATTEMPTS + 1):
        for stale in out_dir.glob("*.wav"):
            stale.unlink()
        try:
            run_with_output(cmd)
        except subprocess.CalledProcessError as exc:
            last_error = exc.stderr.decode(errors="replace")[-2000:]
            continue

        generated = sorted(out_dir.glob(f"{prefix}_*.wav"))
        if not generated:
            generated = sorted(out_dir.glob("*.wav"))
        if not generated:
            last_error = f"mlx-audio did not create a wav file in {out_dir}"
            continue
        wav_path.write_bytes(generated[0].read_bytes())
        try:
            validate_wav_segment(wav_path, text, lang_code)
        except ValueError as exc:
            last_error = f"attempt {attempt}: {exc}"
            if attempt == MAX_TTS_ATTEMPTS:
                die(last_error)
            continue
        return
    die(f"mlx-audio generation failed for {lang_code}: {last_error}")


def silence_frames(params: tuple[int, int, int, int, str, str], seconds: float) -> bytes:
    nchannels, sampwidth, framerate, _nframes, _comptype, _compname = params
    frame_count = int(framerate * seconds)
    return b"\x00" * frame_count * nchannels * sampwidth


def slow_down_wav(input_wav: Path, output_wav: Path, tempo: float) -> None:
    if tempo <= 0:
        die("--ja-tempo must be greater than 0")
    if abs(tempo - 1.0) < 0.001:
        if input_wav != output_wav:
            output_wav.write_bytes(input_wav.read_bytes())
        return
    require_command("ffmpeg")
    run([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_wav),
        "-filter:a",
        f"atempo={tempo}",
        str(output_wav),
    ])


def trim_wav_silence(input_wav: Path, output_wav: Path) -> None:
    """Trim accidental long leading/trailing TTS silence from a generated segment."""
    require_command("ffmpeg")
    run([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_wav),
        "-af",
        "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-50dB:"
        "stop_periods=1:stop_duration=0.8:stop_threshold=-50dB",
        str(output_wav),
    ])


def concat_wavs(parts: list[tuple[Path, float]], output_wav: Path) -> None:
    if not parts:
        die("no audio parts generated")

    first = wave.open(str(parts[0][0]), "rb")
    params = first.getparams()
    first.close()

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_wav), "wb") as out:
        out.setparams(params)
        for index, (part, pause_after_seconds) in enumerate(parts):
            with wave.open(str(part), "rb") as src:
                if src.getparams()[:3] != params[:3]:
                    die(f"audio parameters differ for {part}")
                out.writeframes(src.readframes(src.getnframes()))
            if index != len(parts) - 1 and pause_after_seconds > 0:
                out.writeframes(silence_frames(params, pause_after_seconds))


def convert_if_needed(output_wav: Path, final_output: Path) -> None:
    suffix = final_output.suffix.lower()
    if suffix == ".wav":
        if output_wav != final_output:
            final_output.write_bytes(output_wav.read_bytes())
        return
    if suffix in {".m4a", ".mp4"}:
        run(["afconvert", "-f", "m4af", "-d", "aac", str(output_wav), str(final_output)])
        return
    if suffix == ".mp3":
        require_command("ffmpeg")
        run([
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output_wav),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(final_output),
        ])
        return
    die("output extension must be .wav, .m4a, .mp4, or .mp3")


def main() -> None:
    parser = argparse.ArgumentParser(description="Make sequential JP/KR lesson audio for Elle")
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tts-backend", choices=["mlx", "macos"], default=DEFAULT_TTS_BACKEND)
    parser.add_argument("--ja-voice", default=DEFAULT_JA_VOICE, help="macOS Japanese voice when --tts-backend macos")
    parser.add_argument("--ko-voice", default=DEFAULT_KO_VOICE, help="macOS Korean voice when --tts-backend macos")
    parser.add_argument("--ja-rate", default=DEFAULT_JA_RATE, help="macOS Japanese speech rate")
    parser.add_argument("--ko-rate", default=DEFAULT_KO_RATE, help="macOS Korean speech rate")
    parser.add_argument("--ja-tempo", type=float, default=DEFAULT_JA_TEMPO, help="post-process Japanese audio tempo; 0.9 is 10% slower")
    parser.add_argument("--mlx-python", default=DEFAULT_MLX_PYTHON)
    parser.add_argument("--mlx-model", default=DEFAULT_MLX_MODEL)
    parser.add_argument("--mlx-voice", default=DEFAULT_MLX_VOICE, help="MLX voice for both languages; overrides per-language defaults")
    parser.add_argument("--mlx-ko-voice", default=DEFAULT_MLX_KO_VOICE, help="MLX voice for Korean meanings")
    parser.add_argument("--mlx-ja-voice", default=DEFAULT_MLX_JA_VOICE, help="MLX voice for Japanese sentences")
    parser.add_argument("--mlx-temperature", type=float, default=DEFAULT_MLX_TEMPERATURE)
    parser.add_argument("--mlx-instruct", default=DEFAULT_MLX_INSTRUCT)
    parser.add_argument("--mlx-max-tokens", type=int, default=DEFAULT_MLX_MAX_TOKENS, help="cap generated audio tokens to prevent runaway MLX output")
    parser.add_argument("--mlx-speed", type=float, default=None, help="model-specific speech speed")
    parser.add_argument("--mlx-duration-multiplier", type=float, default=None, help="model-specific duration multiplier")
    parser.add_argument("--mlx-repetition-penalty", type=float, default=DEFAULT_MLX_REPETITION_PENALTY, help="model repetition penalty")
    parser.add_argument(
        "--between-lang-pause",
        type=float,
        default=DEFAULT_BETWEEN_LANG_PAUSE,
        help="pause after Korean meaning before Japanese sentence",
    )
    parser.add_argument(
        "--between-items-pause",
        type=float,
        default=DEFAULT_BETWEEN_ITEMS_PAUSE,
        help="pause after the last Japanese repetition before the next Korean meaning",
    )
    args = parser.parse_args()

    if args.tts_backend == "macos":
        require_command("say")
        require_command("afconvert")
    else:
        if not Path(args.mlx_python).exists():
            die(f"mlx python not found: {args.mlx_python}")

    items = load_items(args.input_json)
    mlx_ko_voice = args.mlx_voice or args.mlx_ko_voice
    mlx_ja_voice = args.mlx_voice or args.mlx_ja_voice
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="elle-tts-") as tmp:
        tmp_dir = Path(tmp)
        parts: list[tuple[Path, float]] = []
        for idx, item in enumerate(items, start=1):
            jp_wav = tmp_dir / f"{idx:02d}-ja.wav"
            ko_wav = tmp_dir / f"{idx:02d}-ko.wav"
            if args.tts_backend == "macos":
                say_to_wav(item["japanese_tts"], args.ja_voice, args.ja_rate, jp_wav, tmp_dir)
                say_to_wav(item["korean"], args.ko_voice, args.ko_rate, ko_wav, tmp_dir)
            else:
                mlx_to_wav(
                    item["japanese_tts"],
                    "Japanese",
                    jp_wav,
                    tmp_dir,
                    mlx_python=args.mlx_python,
                    mlx_model=args.mlx_model,
                    mlx_voice=mlx_ja_voice,
                    mlx_temperature=args.mlx_temperature,
                    mlx_instruct=args.mlx_instruct,
                    mlx_max_tokens=args.mlx_max_tokens,
                    mlx_speed=args.mlx_speed,
                    mlx_duration_multiplier=args.mlx_duration_multiplier,
                    mlx_repetition_penalty=args.mlx_repetition_penalty,
                )
                mlx_to_wav(
                    item["korean"],
                    "Korean",
                    ko_wav,
                    tmp_dir,
                    mlx_python=args.mlx_python,
                    mlx_model=args.mlx_model,
                    mlx_voice=mlx_ko_voice,
                    mlx_temperature=args.mlx_temperature,
                    mlx_instruct=args.mlx_instruct,
                    mlx_max_tokens=args.mlx_max_tokens,
                    mlx_speed=args.mlx_speed,
                    mlx_duration_multiplier=args.mlx_duration_multiplier,
                    mlx_repetition_penalty=args.mlx_repetition_penalty,
                )
            ko_trimmed_wav = tmp_dir / f"{idx:02d}-ko-trimmed.wav"
            trim_wav_silence(ko_wav, ko_trimmed_wav)
            validate_wav_segment(ko_trimmed_wav, item["korean"], "Korean")
            ko_wav = ko_trimmed_wav

            jp_trimmed_wav = tmp_dir / f"{idx:02d}-ja-trimmed.wav"
            trim_wav_silence(jp_wav, jp_trimmed_wav)
            validate_wav_segment(jp_trimmed_wav, item["japanese_tts"], "Japanese")
            jp_wav = jp_trimmed_wav

            if abs(args.ja_tempo - 1.0) >= 0.001:
                jp_slow_wav = tmp_dir / f"{idx:02d}-ja-tempo.wav"
                slow_down_wav(jp_wav, jp_slow_wav, args.ja_tempo)
                jp_wav = jp_slow_wav

            parts.append((ko_wav, args.between_lang_pause))
            parts.append((jp_wav, args.between_lang_pause))
            parts.append((jp_wav, args.between_lang_pause))
            parts.append((jp_wav, args.between_items_pause))

        combined_wav = tmp_dir / "combined.wav"
        concat_wavs(parts, combined_wav)
        convert_if_needed(combined_wav, args.output)

    print(str(args.output))


if __name__ == "__main__":
    main()
