#!/usr/bin/env python3
"""Generate the bundled PiBells default audio sample pack.

The emergency voice prompts are intentionally generic default samples. Districts
can replace them in the PiBells audio library with locally approved recordings.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = ROOT / "audio"
SAMPLE_RATE = 44_100
VOICE = "Samantha"
VOICE_RATE = "168"


BELL_SAMPLES = {
    "bell-start-warm-chime.mp3": {
        "name": "Day Start Warm Chime",
        "notes": [(523.25, 0.42), (659.25, 0.42), (783.99, 0.52), (1046.5, 0.72)],
    },
    "bell-passing-classic.mp3": {
        "name": "Passing Bell Classic",
        "notes": [(880.0, 0.56), (659.25, 0.46), (880.0, 0.68)],
    },
    "bell-passing-soft-chime.mp3": {
        "name": "Passing Bell Soft Chime",
        "notes": [(659.25, 0.34), (783.99, 0.34), (987.77, 0.5)],
    },
    "bell-lunch-light-chime.mp3": {
        "name": "Lunch Light Chime",
        "notes": [(783.99, 0.32), (987.77, 0.32), (1174.66, 0.46), (987.77, 0.42)],
    },
    "bell-dismissal-deep-chime.mp3": {
        "name": "Dismissal Deep Chime",
        "notes": [(392.0, 0.46), (523.25, 0.46), (659.25, 0.54), (783.99, 0.78)],
    },
    "bell-test-tone.mp3": {
        "name": "PiBells Test Tone",
        "notes": [(880.0, 0.18), (0.0, 0.08), (880.0, 0.18)],
    },
}


EMERGENCY_SAMPLES = {
    "emergency-general.mp3": {
        "tone": "critical",
        "text": (
            "Attention. Emergency response in progress. Staff, follow emergency protocol "
            "and await instructions. Repeat. Emergency response in progress."
        ),
    },
    "emergency-hold.mp3": {
        "tone": "high",
        "text": (
            "Hold in your room or area. Clear the halls. Continue instruction until released. "
            "Repeat. Hold in your room or area."
        ),
    },
    "emergency-secure.mp3": {
        "tone": "high",
        "text": (
            "Secure. Get inside. Lock outside doors. Students and staff outside, move inside now. "
            "Repeat. Secure."
        ),
    },
    "emergency-lockdown.mp3": {
        "tone": "critical",
        "text": (
            "Lockdown. Lockdown. Locks, lights, out of sight. Remain silent. Wait for authorized "
            "release. Repeat. Lockdown."
        ),
    },
    "emergency-evacuate.mp3": {
        "tone": "critical",
        "text": (
            "Evacuate. Evacuate to the announced assembly area. Follow staff directions and account "
            "for your group. Repeat. Evacuate."
        ),
    },
    "emergency-shelter.mp3": {
        "tone": "high",
        "text": (
            "Shelter. Move to the directed shelter area. Await hazard specific instructions. "
            "Repeat. Shelter."
        ),
    },
    "emergency-medical.mp3": {
        "tone": "medical",
        "text": (
            "Medical response team to the announced location. Staff, keep nearby areas clear. "
            "Repeat. Medical response."
        ),
    },
    "emergency-all-clear.mp3": {
        "tone": "clear",
        "text": (
            "All clear. Resume normal operations. Staff, follow your check in procedure. "
            "Repeat. All clear."
        ),
    },
}


def sine(freq: float, seconds: float, amp: float = 0.42) -> list[float]:
    frames = int(SAMPLE_RATE * seconds)
    if freq <= 0:
        return [0.0] * frames

    samples: list[float] = []
    fade_frames = max(1, int(SAMPLE_RATE * 0.025))
    for index in range(frames):
        env = 1.0
        if index < fade_frames:
            env = index / fade_frames
        elif frames - index < fade_frames:
            env = (frames - index) / fade_frames
        value = math.sin(2 * math.pi * freq * index / SAMPLE_RATE)
        value += 0.22 * math.sin(2 * math.pi * freq * 2 * index / SAMPLE_RATE)
        value += 0.09 * math.sin(2 * math.pi * freq * 3 * index / SAMPLE_RATE)
        samples.append(value * amp * env)
    return samples


def dual_tone(first: float, second: float, seconds: float, amp: float = 0.38) -> list[float]:
    a = sine(first, seconds, amp)
    b = sine(second, seconds, amp * 0.78)
    return [max(-1.0, min(1.0, x + y)) for x, y in zip(a, b)]


def silence(seconds: float) -> list[float]:
    return [0.0] * int(SAMPLE_RATE * seconds)


def join(parts: Iterable[list[float]]) -> list[float]:
    output: list[float] = []
    for part in parts:
        output.extend(part)
    return output


def normalize(samples: list[float], target: float = 0.88) -> list[float]:
    peak = max((abs(sample) for sample in samples), default=0.0)
    if peak <= 0:
        return samples
    scale = min(target / peak, 1.0)
    return [max(-1.0, min(1.0, sample * scale)) for sample in samples]


def write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        data = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767)) for sample in samples)
        handle.writeframes(data)


def read_wav(path: Path) -> list[float]:
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
        channels = handle.getnchannels()
    if width != 2:
        raise RuntimeError(f"Unsupported sample width for {path}: {width}")
    raw = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    if channels == 1:
        return [sample / 32768 for sample in raw]
    return [
        sum(raw[index:index + channels]) / channels / 32768
        for index in range(0, len(raw), channels)
    ]


def say_samples(text: str, workspace: Path) -> list[float]:
    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if not say or not ffmpeg:
        raise RuntimeError("Generating voice samples requires macOS 'say' and ffmpeg.")

    aiff_path = workspace / "voice.aiff"
    wav_path = workspace / "voice.wav"
    subprocess.run([say, "-v", VOICE, "-r", VOICE_RATE, "-o", str(aiff_path), text], check=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff_path), "-ar", str(SAMPLE_RATE), "-ac", "1", str(wav_path)],
        check=True,
    )
    return read_wav(wav_path)


def encode_mp3(samples: list[float], output_path: Path, workspace: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Encoding MP3 samples requires ffmpeg.")

    wav_path = workspace / f"{output_path.stem}.wav"
    write_wav(wav_path, normalize(samples))
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "160k",
            str(output_path),
        ],
        check=True,
    )


def bell_samples(notes: list[tuple[float, float]]) -> list[float]:
    parts: list[list[float]] = []
    for freq, duration in notes:
        if freq <= 0:
            parts.append(silence(duration))
        else:
            parts.append(dual_tone(freq, freq * 1.5, duration))
            parts.append(silence(0.07))
    parts.append(silence(0.32))
    return join(parts)


def emergency_intro(tone: str) -> list[float]:
    if tone == "critical":
        return join([
            dual_tone(430, 645, 0.54, 0.5),
            silence(0.09),
            dual_tone(430, 645, 0.54, 0.5),
            silence(0.16),
        ])
    if tone == "medical":
        return join([
            dual_tone(587, 880, 0.28, 0.34),
            silence(0.08),
            dual_tone(784, 1175, 0.28, 0.34),
            silence(0.16),
        ])
    if tone == "clear":
        return join([
            dual_tone(523, 784, 0.34, 0.28),
            dual_tone(659, 988, 0.34, 0.28),
            dual_tone(784, 1175, 0.52, 0.26),
            silence(0.16),
        ])
    return join([
        dual_tone(740, 1110, 0.24, 0.34),
        silence(0.07),
        dual_tone(740, 1110, 0.24, 0.34),
        silence(0.12),
    ])


def generate() -> None:
    AUDIO_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pibells-audio-") as temp:
        workspace = Path(temp)

        for filename, config in BELL_SAMPLES.items():
            encode_mp3(bell_samples(config["notes"]), AUDIO_DIR / filename, workspace)
            print(f"wrote {filename}")

        for filename, config in EMERGENCY_SAMPLES.items():
            voice = say_samples(config["text"], workspace)
            samples = join([
                emergency_intro(config["tone"]),
                voice,
                silence(0.28),
            ])
            encode_mp3(samples, AUDIO_DIR / filename, workspace)
            print(f"wrote {filename}")


if __name__ == "__main__":
    generate()
