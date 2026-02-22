"""Voice message transcription via OpenAI Whisper API."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib import error, request

import structlog
from telegram import Voice

from src.config import Settings

logger = structlog.get_logger(__name__)


@dataclass
class VoiceTranscriptionResult:
    """Transcription metadata for a processed Telegram voice message."""

    text: str
    chunk_count: int
    original_size_bytes: int


class WhisperVoiceHandler:
    """Transcribe Telegram voice messages with OpenAI Whisper."""

    API_URL = "https://api.openai.com/v1/audio/transcriptions"
    MODEL = "whisper-1"
    MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # Keep a little headroom under API limits

    def __init__(self, config: Settings):
        self.config = config
        self.api_key = config.whisper_api_key_str
        if not self.api_key:
            raise ValueError("WHISPER_API_KEY is not configured")

        self.temp_dir = Path("/tmp/codex_bot_voice")
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def transcribe_voice(self, voice: Voice) -> VoiceTranscriptionResult:
        """Download, split if needed, and transcribe a Telegram voice message."""
        source_file = await self._download_voice_file(voice)
        chunk_paths: List[Path] = []

        try:
            source_size = source_file.stat().st_size
            if source_size <= self.MAX_UPLOAD_BYTES:
                chunk_paths = [source_file]
            else:
                chunk_paths = await self._split_for_whisper(
                    source_file,
                    duration_seconds=getattr(voice, "duration", None),
                    source_size_bytes=source_size,
                )

            transcribed_parts: List[str] = []
            for idx, chunk_path in enumerate(chunk_paths, start=1):
                logger.info(
                    "Transcribing voice chunk",
                    chunk_index=idx,
                    chunk_count=len(chunk_paths),
                    chunk_size_bytes=chunk_path.stat().st_size,
                )
                text = await self._transcribe_chunk(chunk_path)
                if text:
                    transcribed_parts.append(text)

            full_text = "\n".join(part.strip() for part in transcribed_parts).strip()
            return VoiceTranscriptionResult(
                text=full_text,
                chunk_count=len(chunk_paths),
                original_size_bytes=source_size,
            )
        finally:
            for path in chunk_paths:
                if path != source_file:
                    path.unlink(missing_ok=True)
            source_file.unlink(missing_ok=True)

    async def _download_voice_file(self, voice: Voice) -> Path:
        """Download Telegram voice file to a temporary local path."""
        file = await voice.get_file()
        suffix = self._infer_suffix(file.file_path)
        path = self.temp_dir / f"voice_{uuid.uuid4().hex}{suffix}"
        await file.download_to_drive(str(path))
        return path

    async def _split_for_whisper(
        self,
        source_file: Path,
        duration_seconds: Optional[int],
        source_size_bytes: int,
    ) -> List[Path]:
        """Split long voice messages into Whisper-safe chunks using ffmpeg."""
        chunk_duration = self._estimate_chunk_duration(
            duration_seconds=duration_seconds,
            source_size_bytes=source_size_bytes,
        )
        pattern = self.temp_dir / f"{source_file.stem}_chunk_%03d.mp3"

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_file),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_duration),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(pattern),
        ]

        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(
                "Failed to split long voice message with ffmpeg. "
                "Install ffmpeg or send a shorter voice note. "
                f"Details: {stderr[:200]}"
            )

        chunks = sorted(self.temp_dir.glob(f"{source_file.stem}_chunk_*.mp3"))
        if not chunks:
            raise RuntimeError("Failed to split long voice message into chunks.")

        oversized = [p.name for p in chunks if p.stat().st_size > self.MAX_UPLOAD_BYTES]
        if oversized:
            raise RuntimeError(
                "Voice chunks are still too large for Whisper upload limits. "
                "Please send a shorter voice message."
            )

        return chunks

    @staticmethod
    def _estimate_chunk_duration(
        duration_seconds: Optional[int], source_size_bytes: int
    ) -> int:
        """Estimate safe ffmpeg segment duration from message duration/size."""
        if not duration_seconds or duration_seconds <= 0:
            return 600

        bytes_per_second = max(1.0, source_size_bytes / duration_seconds)
        target_bytes = WhisperVoiceHandler.MAX_UPLOAD_BYTES * 0.8
        estimated = int(target_bytes / bytes_per_second)
        return max(30, min(900, estimated))

    async def _transcribe_chunk(self, file_path: Path) -> str:
        """Send one audio file chunk to Whisper transcription endpoint."""
        payload, content_type = self._build_multipart(file_path)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": content_type,
        }

        req = request.Request(
            self.API_URL,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            raw = await asyncio.to_thread(self._urlopen_read, req)
        except error.HTTPError as http_err:
            detail = ""
            try:
                detail = http_err.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(http_err)
            raise RuntimeError(
                f"Whisper API request failed ({http_err.code}): {detail[:300]}"
            ) from http_err
        except error.URLError as url_err:
            raise RuntimeError(
                f"Could not connect to Whisper API: {url_err.reason}"
            ) from url_err

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as decode_err:
            raise RuntimeError("Whisper API returned invalid JSON.") from decode_err

        text = (parsed.get("text") or "").strip()
        if not text:
            raise RuntimeError("Whisper API returned an empty transcription.")
        return text

    @staticmethod
    def _urlopen_read(req: request.Request) -> bytes:
        with request.urlopen(req, timeout=180) as resp:
            return resp.read()

    def _build_multipart(self, file_path: Path) -> tuple[bytes, str]:
        """Build multipart/form-data payload for Whisper transcription request."""
        boundary = f"----CodexBoundary{uuid.uuid4().hex}"
        filename = file_path.name
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_bytes = file_path.read_bytes()

        parts: List[bytes] = []
        parts.append(self._form_field(boundary, "model", self.MODEL))
        parts.append(self._form_field(boundary, "response_format", "json"))
        parts.append(
            self._file_field(
                boundary=boundary,
                name="file",
                filename=filename,
                content_type=mime_type,
                data=file_bytes,
            )
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))

        payload = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
        return payload, content_type

    @staticmethod
    def _form_field(boundary: str, name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    @staticmethod
    def _file_field(
        boundary: str,
        name: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> bytes:
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        return header + data + b"\r\n"

    @staticmethod
    def _infer_suffix(file_path: Optional[str]) -> str:
        if not file_path:
            return ".ogg"
        suffix = Path(file_path).suffix
        return suffix if suffix else ".ogg"
