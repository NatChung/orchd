"""Bounded media pre-processing: ffprobe/ffmpeg keyframes and audio track.

Inputs are untrusted. ffmpeg/ffprobe run via argv lists (never a shell) with a
timeout. Failures return a MediaResult with a stable ``reason`` instead of
raising. Speech-to-text is a pluggable Transcriber protocol; none is bundled.
"""
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .attachments import content_path

WINGET_HINT = 'ffmpeg not found; install with: winget install --id Gyan.FFmpeg -e (then restart the shell)'


@dataclass(frozen=True)
class MediaLimits:
    max_duration_s: float = 600.0
    max_frames: int = 8
    max_width: int = 1280
    timeout_s: float = 60.0
    frame_mode: str = 'interval'  # 'interval' (equal spacing) or 'scene'
    audio_format: str = 'wav'  # 'wav' or 'm4a'
    retention_s: int = 7 * 24 * 3600

    def validate(self):
        if self.frame_mode not in ('interval', 'scene') or self.audio_format not in ('wav', 'm4a'):
            raise ValueError('invalid media limits')
        if self.max_frames < 1 or self.max_width < 16 or self.timeout_s <= 0 or self.max_duration_s <= 0:
            raise ValueError('invalid media limits')


@dataclass
class MediaResult:
    ok: bool
    reason: str = ''
    detail: str = ''
    duration_s: float = 0.0
    frames: list = field(default_factory=list)
    audio: str = ''
    output_dir: str = ''


class Transcriber(Protocol):
    def transcribe(self, audio_path: str, *, timeout_s: float) -> list:
        """Return [{'start': float, 'end': float, 'text': str}, ...]."""


def transcribe(audio_path, transcriber=None, timeout_s=60.0):
    if transcriber is None:
        return MediaResult(False, 'transcriber_unconfigured', 'no speech-to-text provider configured')
    try:
        segments = transcriber.transcribe(str(audio_path), timeout_s=timeout_s)
    except Exception as exc:  # provider is external; never crash the caller
        return MediaResult(False, 'transcriber_failed', type(exc).__name__)
    return MediaResult(True, detail=json.dumps(list(segments)))


def derived_dir(channel_home, source_id, digest):
    """Output dir beside the attachment content file (validated like content_path)."""
    path = content_path(channel_home, source_id, digest)
    return path.with_name(digest + '.derived')


def purge_expired(directory, retention_s, now=None):
    """Remove derived files older than retention; returns count removed."""
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for item in root.iterdir():
        try:
            if not item.is_symlink() and item.is_file() and now - item.stat().st_mtime > retention_s:
                item.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _find(name):
    return shutil.which(name)


def _run(argv, timeout_s):
    return subprocess.run(argv, capture_output=True, timeout=timeout_s, shell=False, check=False)


def process_media(input_path, out_dir, limits=None, *, ffmpeg=None, ffprobe=None, runner=None, now=None):
    limits = limits or MediaLimits()
    limits.validate()
    runner = runner or _run
    ffmpeg = ffmpeg or _find('ffmpeg')
    ffprobe = ffprobe or _find('ffprobe')
    if not ffmpeg or not ffprobe:
        return MediaResult(False, 'ffmpeg_unavailable', WINGET_HINT)
    src = Path(input_path)
    if src.is_symlink() or not src.is_file():
        return MediaResult(False, 'input_missing')
    out = Path(out_dir)
    if out.is_symlink():
        return MediaResult(False, 'unsafe_output_dir')
    purge_expired(out, limits.retention_s, now)
    try:
        probe = runner([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '--', str(src)], limits.timeout_s)
    except subprocess.TimeoutExpired:
        return MediaResult(False, 'timeout', 'ffprobe')
    except OSError:
        return MediaResult(False, 'ffmpeg_unavailable', WINGET_HINT)
    try:
        if probe.returncode != 0:
            raise ValueError
        duration = float(json.loads(probe.stdout)['format']['duration'])
        if not duration > 0 or duration == float('inf'):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return MediaResult(False, 'probe_failed')
    if duration > limits.max_duration_s:
        return MediaResult(False, 'duration_exceeded', f'{duration:.1f}s > {limits.max_duration_s:.1f}s', duration)
    out.mkdir(parents=True, exist_ok=True)
    scale = f'scale=min({limits.max_width}\\,iw):-2'
    if limits.frame_mode == 'scene':
        vf = f"select='gt(scene,0.3)',{scale}"
        vsync = ['-vsync', 'vfr']
    else:
        vf = f'fps={limits.max_frames / duration:.6f},{scale}'
        vsync = []
    pattern = out / 'frame_%03d.png'
    audio = out / f'audio.{limits.audio_format}'
    codec = ['-c:a', 'pcm_s16le', '-ar', '16000', '-ac', '1'] if limits.audio_format == 'wav' else ['-c:a', 'aac']
    frame_cmd = [ffmpeg, '-nostdin', '-y', '-v', 'error', '-i', str(src), '-an', '-vf', vf, *vsync,
                 '-frames:v', str(limits.max_frames), str(pattern)]
    audio_cmd = [ffmpeg, '-nostdin', '-y', '-v', 'error', '-i', str(src), '-vn', *codec, str(audio)]
    deadline = time.monotonic() + limits.timeout_s
    for label, cmd in (('frames', frame_cmd), ('audio', audio_cmd)):
        try:
            res = runner(cmd, max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return MediaResult(False, 'timeout', label, duration, output_dir=str(out))
        except OSError:
            return MediaResult(False, 'ffmpeg_unavailable', WINGET_HINT)
        # A missing audio stream is tolerated; a frame-extraction failure is not.
        if res.returncode != 0 and label == 'frames':
            return MediaResult(False, 'ffmpeg_failed', label, duration, output_dir=str(out))
    frames = sorted(str(p) for p in out.glob('frame_*.png'))[:limits.max_frames]
    return MediaResult(True, duration_s=duration, frames=frames,
                       audio=str(audio) if audio.is_file() else '', output_dir=str(out))
