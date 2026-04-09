"""
Audio processor — FFmpeg-based conversion and validation.
Converts any meeting format (mp4, mkv, webm, m4a, wav, ogg …) to MP3.

Uses subprocess.run inside asyncio.to_thread() instead of
asyncio.create_subprocess_exec to avoid Windows SelectorEventLoop
NotImplementedError when running under uvicorn --reload.
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import aiofiles

from src.config import get_settings
from src.observability.logging import get_logger

log = get_logger(__name__)
settings = get_settings()

ALLOWED_MIME_PREFIXES = ("audio/", "video/")
ALLOWED_EXTENSIONS = {
    ".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".webm",
    ".mkv", ".avi", ".mov", ".flac", ".aac",
}


class AudioProcessingError(Exception):
    pass


async def save_upload(file_bytes: bytes, original_filename: str) -> tuple[str, str]:
    """
    Save raw upload bytes to disk using a UUID-based filename (prevents path traversal).
    Returns (stored_filename, full_path).
    """
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AudioProcessingError(
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    stored_name = f"{uuid.uuid4()}{ext}"
    dest_path = settings.upload_dir / stored_name

    async with aiofiles.open(dest_path, "wb") as f:
        await f.write(file_bytes)

    log.info("upload_saved", filename=stored_name, size_bytes=len(file_bytes))
    return stored_name, str(dest_path)


def _run_ffmpeg(source_path: str, out_path: str) -> None:
    """Synchronous FFmpeg call — meant to be run inside asyncio.to_thread()."""
    cmd = [
        "ffmpeg",
        "-y",                       # overwrite without asking
        "-i", source_path,          # input
        "-vn",                      # drop video stream
        "-ac", "1",                 # mono
        "-ar", "16000",             # 16kHz — optimal for Whisper
        "-b:a", "64k",              # 64kbps — good quality, small file
        "-f", "mp3",
        out_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError:
        raise AudioProcessingError(
            "FFmpeg is required but not installed. "
            "Install it and add to your system PATH."
        )
    except subprocess.TimeoutExpired:
        raise AudioProcessingError("FFmpeg conversion timed out after 300 seconds.")

    if result.returncode != 0:
        raise AudioProcessingError(
            f"FFmpeg failed (exit {result.returncode}): {result.stderr.decode()[:300]}"
        )


async def extract_audio_as_mp3(source_path: str) -> str:
    """
    Convert any audio/video file to a 16kHz mono MP3 using FFmpeg.
    Returns path to the converted MP3 file.
    Runs FFmpeg in a thread to avoid Windows event loop issues.
    """
    out_path = source_path.rsplit(".", 1)[0] + "_converted.mp3"
    log.info("ffmpeg_converting", source=source_path, dest=out_path)

    await asyncio.to_thread(_run_ffmpeg, source_path, out_path)

    log.info("ffmpeg_done", output=out_path)
    return out_path


def _run_ffprobe(audio_path: str) -> float:
    """Synchronous FFprobe call — meant to be run inside asyncio.to_thread()."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return float(result.stdout.decode().strip())


async def get_audio_duration(audio_path: str) -> float:
    """
    Use FFprobe to get the duration of an audio file in seconds.
    Falls back to 0.0 on failure.
    """
    try:
        return await asyncio.to_thread(_run_ffprobe, audio_path)
    except Exception as exc:
        log.warning("ffprobe_failed", error=str(exc))
        return 0.0


def cleanup_files(*paths: str) -> None:
    """Delete temporary converted files to free disk space."""
    for path in paths:
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
                log.debug("cleanup_deleted", path=path)
        except Exception as exc:
            log.warning("cleanup_failed", path=path, error=str(exc))

