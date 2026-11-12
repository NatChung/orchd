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
    max_height: int = 720
    max_pixels: int = 1280 * 720
    timeout_s: float = 60.0
    frame_mode: str = 'interval'  # 'interval' (equal spacing) or 'scene'
    audio_format: str = 'wav'  # 'wav' or 'm4a'
    retention_s: int = 7 * 24 * 3600

    def validate(self):
        if self.frame_mode not in ('interval', 'scene') or self.audio_format not in ('wav', 'm4a'):
            raise ValueError('invalid media limits')
        if self.max_frames < 1 or self.max_width < 16 or self.max_height < 16 or self.max_pixels < 256 or self.timeout_s <= 0 or self.max_duration_s <= 0:
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


def purge_all(channel_home, retention_s, now=None):
    """Purge expired derived files under every <digest>.derived dir of a channel home.

    Independent of ffmpeg availability; callable from any periodic job. Symlinked
    directories are never followed. Returns the number of files removed.
    """
    state = Path(channel_home).absolute() / 'email' / 'state'
    removed = 0
    try:
        if state.is_symlink() or not state.is_dir():
            return 0
        sources = [d for d in state.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return 0
    for source in sources:
        content = source / 'content'
        try:
            if content.is_symlink() or not content.is_dir():
                continue
            derived = [d for d in content.iterdir() if d.name.endswith('.derived') and d.is_dir() and not d.is_symlink()]
        except OSError:
            continue
        for d in derived:
            removed += purge_expired(d, retention_s, now)
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                pass
    return removed


def _find(name):
    return shutil.which(name)


def _run(argv, timeout_s):
    return subprocess.run(argv, capture_output=True, timeout=timeout_s, shell=False, check=False)


def _safe_out_dir(out_dir, allowed_root):
    """Resolve out_dir fully (all ancestors) and confine it to allowed_root.

    allowed_root must be supplied by the caller. Returns the resolved Path or None.
    """
    if allowed_root is None:
        return None
    out = Path(out_dir)
    try:
        if out.is_symlink():
            return None
        resolved = out.resolve()
        root = Path(allowed_root).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved


def _clear_outputs(out):
    """Remove prior frame/audio outputs (never following links) so none can be reused or overwritten through."""
    for pattern in ('frame_*.png', 'audio.*'):
        for p in out.glob(pattern):
            if p.is_dir() and not p.is_symlink():
                raise OSError('unexpected directory in output dir')
            p.unlink()


def _scale_filter(limits):
    # factor <= 1 satisfying width, height and total-pixel caps; keep even dims.
    f = (f'min(1,min({limits.max_width}/iw,min({limits.max_height}/ih,'
         f'sqrt({limits.max_pixels}/(iw*ih)))))')
    w = f'max(2,trunc(iw*{f}/2)*2)'
    h = f'min({limits.max_pixels // 4 * 2},max(2,trunc(ih*{f}/2)*2))'
    # Clamp again after the minimum even dimension: a 1-pixel side becomes 2.
    return (f"scale=w='{w}':h='{h}',"
            f"scale=w='min(iw,trunc({limits.max_pixels}/ih/2)*2)':h=ih")


def process_media(input_path, out_dir, limits=None, *, ffmpeg=None, ffprobe=None, runner=None, now=None,
                  allowed_root):
    """Extract inside a caller-supplied trusted root.

    The root and all output ancestors must be owned by the caller and must not
    be writable/replaced by untrusted users or concurrent tasks during this call.
    Resolve checks before cleanup and each subprocess catch accidental swaps;
    they do not close the OS-level TOCTOU window. ffmpeg opens paths itself, so
    hostile concurrent directory replacement is outside this API's threat model.
    Callers must serialize processing of the same output directory.
    """
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
    out = _safe_out_dir(out_dir, allowed_root)
    if out is None:
        return MediaResult(False, 'unsafe_output_dir')
    purge_expired(out, limits.retention_s, now)
    # One budget shared by the probe and both extractions.
    deadline = time.monotonic() + limits.timeout_s

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise subprocess.TimeoutExpired('media', limits.timeout_s)
        return budget

    try:
        probe = runner([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams',
                        '--', str(src)], remaining())
    except subprocess.TimeoutExpired:
        return MediaResult(False, 'timeout', 'ffprobe')
    except OSError:
        return MediaResult(False, 'ffmpeg_unavailable', WINGET_HINT)
    try:
        if probe.returncode != 0:
            raise ValueError
        info = json.loads(probe.stdout)
        duration = float(info['format']['duration'])
        if not duration > 0 or duration == float('inf'):
            raise ValueError
        has_audio = any(isinstance(s, dict) and s.get('codec_type') == 'audio' for s in info.get('streams') or [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return MediaResult(False, 'probe_failed')
    if duration > limits.max_duration_s:
        return MediaResult(False, 'duration_exceeded', f'{duration:.1f}s > {limits.max_duration_s:.1f}s', duration)
    try:
        out.mkdir(parents=True, exist_ok=True)
        if _safe_out_dir(out_dir, allowed_root) != out:
            return MediaResult(False, 'unsafe_output_dir')
        _clear_outputs(out)
    except OSError:
        return MediaResult(False, 'unsafe_output_dir')
    scale = _scale_filter(limits)
    if limits.frame_mode == 'scene':
        vf = f"select='gt(scene,0.3)',{scale}"
        vsync = ['-fps_mode', 'vfr']
    else:
        vf = f'fps={limits.max_frames / duration:.6f},{scale}'
        vsync = []
    pattern = out / 'frame_%03d.png'
    audio = out / f'audio.{limits.audio_format}'
    codec = ['-c:a', 'pcm_s16le', '-ar', '16000', '-ac', '1'] if limits.audio_format == 'wav' else ['-c:a', 'aac']
    # -t before -i caps the input read time regardless of container metadata; -n never overwrites.
    cap = ['-t', f'{limits.max_duration_s:.3f}']
    frame_cmd = [ffmpeg, '-nostdin', '-n', '-v', 'error', *cap, '-i', str(src), '-an', '-vf', vf, *vsync,
                 '-frames:v', str(limits.max_frames), str(pattern)]
    audio_cmd = [ffmpeg, '-nostdin', '-n', '-v', 'error', *cap, '-i', str(src), '-vn', *codec, str(audio)]
    jobs = [('frames', frame_cmd)] + ([('audio', audio_cmd)] if has_audio else [])

    def fail(reason, label):
        try:
            if _safe_out_dir(out_dir, allowed_root) == out:
                _clear_outputs(out)
        except OSError:
            pass
        return MediaResult(False, reason, label, duration, output_dir=str(out))

    for label, cmd in jobs:
        try:
            if _safe_out_dir(out_dir, allowed_root) != out:
                return fail('unsafe_output_dir', label)
            res = runner(cmd, remaining())
            if label == 'audio' and (not audio.is_file() or audio.stat().st_size == 0):
                return fail('ffmpeg_failed', label)
        except subprocess.TimeoutExpired:
            return fail('timeout', label)
        except OSError:
            return fail('ffmpeg_unavailable', WINGET_HINT)
        if res.returncode != 0:
            return fail('ffmpeg_failed', label)
    try:
        frames = sorted(str(p) for p in out.glob('frame_*.png'))[:limits.max_frames]
    except OSError:
        return fail('ffmpeg_failed', 'frames')
    if not frames and limits.frame_mode == 'interval':
        return fail('ffmpeg_failed', 'frames')
    return MediaResult(True, duration_s=duration, frames=frames,
                       audio=str(audio) if has_audio else '', output_dir=str(out))
