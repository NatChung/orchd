import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchd.channels import media
from orchd.channels.media import MediaLimits, process_media as _process_media

def process_media(input_path, out_dir, *args, **kwargs):
    kwargs.setdefault("allowed_root", Path(input_path).parent)
    return _process_media(input_path, out_dir, *args, **kwargs)

def symlinks_available():
    if os.name != "nt":
        return True  # Linux must execute these tests; creation failure is a failure.
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(Path(tmp) / "target", Path(tmp) / "link")
            return True
        except (OSError, NotImplementedError):
            return False

SYMLINKS = symlinks_available()


def fake_runner(duration=10.0, calls=None, timeout_on=None, audio=True, fail_on=None, frames=3):
    seen = []

    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        if timeout_on and timeout_on in argv[0]:
            raise subprocess.TimeoutExpired(argv, timeout)
        seen.append(timeout)
        if 'ffprobe' in argv[0]:
            streams = [{'codec_type': 'video'}] + ([{'codec_type': 'audio'}] if audio else [])
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {'format': {'duration': str(duration)}, 'streams': streams}).encode())
        out = Path(argv[-1])
        if fail_on and fail_on in out.name:
            return SimpleNamespace(returncode=1, stdout=b'')
        if '%03d' in out.name:
            for i in range(1, frames + 1):
                (out.parent / f'frame_{i:03d}.png').write_bytes(b'png')
        else:
            out.write_bytes(b'wav')
        return SimpleNamespace(returncode=0, stdout=b'')
    run.seen = seen
    return run


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / 'in.mp4'
        self.src.write_bytes(b'x')
        self.out = Path(self.tmp.name) / 'out'

    def test_ffmpeg_missing_returns_winget_hint(self):
        orig = media._find
        media._find = lambda name: None
        try:
            r = process_media(self.src, self.out)
        finally:
            media._find = orig
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, 'ffmpeg_unavailable')
        self.assertIn('winget', r.detail)

    def test_duration_exceeded(self):
        r = process_media(self.src, self.out, MediaLimits(max_duration_s=5), ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10))
        self.assertEqual(r.reason, 'duration_exceeded')
        self.assertFalse(self.out.exists())

    def test_timeout(self):
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(timeout_on='ffprobe'))
        self.assertEqual(r.reason, 'timeout')

    def test_probe_failure(self):
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe',
                          runner=lambda a, t: SimpleNamespace(returncode=1, stdout=b''))
        self.assertEqual(r.reason, 'probe_failed')

    def test_extracts_frames_and_audio_without_shell(self):
        calls = []
        r = process_media(self.src, self.out, MediaLimits(max_frames=2), ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10, calls))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(len(r.frames), 2)
        self.assertTrue(r.audio.endswith('audio.wav'))
        self.assertTrue(all(isinstance(c, list) for c in calls))
        self.assertIn('--', calls[0])

    def test_transcriber_unconfigured_and_plugged(self):
        self.assertEqual(media.transcribe('a.wav').reason, 'transcriber_unconfigured')

        class T:
            def transcribe(self, path, *, timeout_s):
                return [{'start': 0, 'end': 1, 'text': 'hi'}]
        self.assertTrue(media.transcribe('a.wav', T()).ok)

    def test_derived_dir_and_retention(self):
        home = Path(self.tmp.name) / 'home'
        d = media.derived_dir(home, 's', 'a' * 64)
        self.assertEqual(d.parent.name, 'content')
        with self.assertRaises(ValueError):
            media.derived_dir(home, '../x', 'a' * 64)
        d.mkdir(parents=True)
        old = d / 'frame_001.png'
        old.write_bytes(b'x')
        os.utime(old, (1, 1))
        self.assertEqual(media.purge_expired(d, 60), 1)
        self.assertFalse(old.exists())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
    def test_real_ffmpeg_integration(self):
        src = Path(self.tmp.name) / 'real.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=d=2:s=160x120:r=10',
                        '-f', 'lavfi', '-i', 'sine=d=2', '-shortest', str(src)], check=True)
        r = process_media(src, self.out, MediaLimits(max_frames=3))
        self.assertTrue(r.ok, r.reason)
        self.assertTrue(r.frames)
        self.assertTrue(Path(r.audio).is_file())


    # --- review rework (#228) ---
    @unittest.skipUnless(SYMLINKS, "symlinks unavailable")
    def test_symlinked_ancestor_output_rejected(self):
        target = Path(self.tmp.name) / 'elsewhere'
        target.mkdir()
        link = Path(self.tmp.name) / 'link'
        os.symlink(target, link, target_is_directory=True)
        calls = []
        r = process_media(self.src, link / 'out', ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10, calls),
                          allowed_root=Path(self.tmp.name) / 'safe')
        self.assertEqual(r.reason, 'unsafe_output_dir')
        self.assertEqual(calls, [])
        self.assertEqual(list(target.iterdir()), [])

    def test_output_outside_allowed_root_rejected(self):
        safe = Path(self.tmp.name) / 'safe'
        safe.mkdir()
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10), allowed_root=safe)
        self.assertEqual(r.reason, 'unsafe_output_dir')
        r = process_media(self.src, safe / 'o', ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10), allowed_root=safe)
        self.assertTrue(r.ok, r.reason)

    @unittest.skipUnless(SYMLINKS, "symlinks unavailable")
    def test_existing_symlinked_output_file_removed_not_followed(self):
        victim = Path(self.tmp.name) / 'victim.txt'
        victim.write_bytes(b'keep')
        self.out.mkdir()
        os.symlink(victim, self.out / 'audio.wav')
        calls = []
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10, calls))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(victim.read_bytes(), b'keep')
        self.assertFalse((self.out / 'audio.wav').is_symlink())
        for c in calls[1:]:
            self.assertIn('-n', c)
            self.assertNotIn('-y', c)

    def test_extraction_has_hard_duration_cap(self):
        calls = []
        r = process_media(self.src, self.out, MediaLimits(max_duration_s=30), ffmpeg='ffmpeg', ffprobe='ffprobe',
                          runner=fake_runner(10, calls))
        self.assertTrue(r.ok, r.reason)
        for c in calls[1:]:
            i = c.index('-t')
            self.assertLess(i, c.index('-i'))  # input option: limits what is read
            self.assertEqual(float(c[i + 1]), 30.0)

    def test_probe_and_extraction_share_one_budget(self):
        run = fake_runner(10)
        process_media(self.src, self.out, MediaLimits(timeout_s=50), ffmpeg='ffmpeg', ffprobe='ffprobe', runner=run)
        self.assertEqual(len(run.seen), 3)
        self.assertTrue(all(b <= a for a, b in zip(run.seen, run.seen[1:])))
        self.assertTrue(all(t <= 50 for t in run.seen))

    def test_purge_all_without_ffmpeg(self):
        home = Path(self.tmp.name) / 'home'
        d1 = media.derived_dir(home, 's1', 'a' * 64)
        d2 = media.derived_dir(home, 's2', 'b' * 64)
        for d in (d1, d2):
            d.mkdir(parents=True)
        old, new = d1 / 'frame_001.png', d2 / 'audio.wav'
        old.write_bytes(b'x')
        new.write_bytes(b'x')
        os.utime(old, (1, 1))
        orig = media._find
        media._find = lambda name: None  # ffmpeg unavailable must not matter
        try:
            self.assertEqual(media.purge_all(home, 60), 1)
        finally:
            media._find = orig
        self.assertFalse(old.exists())
        self.assertFalse(d1.exists())  # emptied dir pruned
        self.assertTrue(new.exists())
        self.assertEqual(media.purge_all(Path(self.tmp.name) / 'missing', 60), 0)

    def test_scale_bounds_width_height_and_pixels(self):
        calls = []
        process_media(self.src, self.out, MediaLimits(max_width=640, max_height=360, max_pixels=100000), ffmpeg='ffmpeg',
                      ffprobe='ffprobe', runner=fake_runner(10, calls))
        vf = calls[1][calls[1].index('-vf') + 1]
        for token in ('640/iw', '360/ih', '100000/(iw*ih)', 'sqrt', 'trunc'):
            self.assertIn(token, vf)
        import math
        iw, ih = 1000, 4000  # tall source
        f = min(1, min(640 / iw, min(360 / ih, math.sqrt(100000 / (iw * ih)))))
        w, h = max(2, int(iw * f / 2) * 2), max(2, int(ih * f / 2) * 2)
        self.assertLessEqual(w, 640)
        self.assertLessEqual(h, 360)
        self.assertLessEqual(w * h, 100000)

    def test_invalid_resolution_limits(self):
        with self.assertRaises(ValueError):
            MediaLimits(max_height=1).validate()
        with self.assertRaises(ValueError):
            MediaLimits(max_pixels=1).validate()

    def test_no_audio_stream_is_not_an_error(self):
        calls = []
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10, calls, audio=False))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.audio, '')
        self.assertEqual(len(calls), 2)  # probe + frames only

    def test_audio_failure_is_error_and_leaves_no_stale_output(self):
        self.out.mkdir()
        (self.out / 'audio.wav').write_bytes(b'stale')
        (self.out / 'frame_009.png').write_bytes(b'stale')
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10, fail_on='audio'))
        self.assertFalse(r.ok)
        self.assertEqual((r.reason, r.detail), ('ffmpeg_failed', 'audio'))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_stale_frames_not_returned_after_partial_run(self):
        self.out.mkdir()
        (self.out / 'frame_009.png').write_bytes(b'stale')
        r = process_media(self.src, self.out, MediaLimits(max_frames=8), ffmpeg='ffmpeg', ffprobe='ffprobe',
                          runner=fake_runner(10, frames=2))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(len(r.frames), 2)
        self.assertFalse((self.out / 'frame_009.png').exists())
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=fake_runner(10, frames=0))
        self.assertEqual(r.reason, 'ffmpeg_failed')
        self.assertEqual(r.frames, [])

    def test_missing_audio_file_after_success_code_is_error(self):
        base = fake_runner(10)

        def run(argv, timeout):
            if 'ffprobe' in argv[0] or '%03d' in argv[-1]:
                return base(argv, timeout)
            return SimpleNamespace(returncode=0, stdout=b'')  # claims success, writes nothing
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=run)
        self.assertEqual((r.reason, r.detail), ('ffmpeg_failed', 'audio'))

    def test_exhausted_shared_deadline_stops_next_process(self):
        for ticks, expected in (([0, 60], 0), ([0, 0, 60], 1), ([0, 0, 1, 60], 2)):
            calls = []
            with patch.object(media.time, 'monotonic', side_effect=ticks):
                r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe',
                                  runner=fake_runner(calls=calls))
            self.assertEqual(r.reason, 'timeout')
            self.assertEqual(len(calls), expected)
            self.assertFalse(list(self.out.glob('frame_*.png')))

    def test_audio_oserror_cleans_partial_frames(self):
        base = fake_runner()
        def run(argv, timeout):
            if argv[-1].endswith('audio.wav'):
                raise OSError('cannot launch')
            return base(argv, timeout)
        r = process_media(self.src, self.out, ffmpeg='ffmpeg', ffprobe='ffprobe', runner=run)
        self.assertEqual(r.reason, 'ffmpeg_unavailable')
        self.assertEqual(list(self.out.iterdir()), [])

    def test_trusted_root_is_required(self):
        with self.assertRaises(TypeError):
            _process_media(self.src, self.out)
        r = process_media(self.src, self.out, allowed_root=None, ffmpeg='ffmpeg', ffprobe='ffprobe')
        self.assertEqual(r.reason, 'unsafe_output_dir')

    @unittest.skipUnless(SYMLINKS, 'symlinks unavailable')
    def test_directory_swap_after_mkdir_is_rejected(self):
        target = Path(self.tmp.name) / 'outside'
        target.mkdir()
        safe = Path(self.tmp.name) / 'safe'
        safe.mkdir()
        out = safe / 'out'
        original = Path.mkdir
        def mkdir(path, *args, **kwargs):
            if path == out:
                os.symlink(target, out, target_is_directory=True)
            else:
                original(path, *args, **kwargs)
        calls = []
        with patch.object(Path, 'mkdir', mkdir):
            r = process_media(self.src, out, allowed_root=safe, ffmpeg='ffmpeg', ffprobe='ffprobe',
                              runner=fake_runner(calls=calls))
        self.assertEqual(r.reason, 'unsafe_output_dir')
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_service_startup_purges_idle_media_without_ffmpeg(self):
        from orchd.channels.service import ChannelService
        home = Path(self.tmp.name) / 'home'
        old = media.derived_dir(home, 'idle', 'a' * 64) / 'frame_001.png'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'expired')
        os.utime(old, (1, 1))
        with patch.object(media, '_find', return_value=None):
            svc = ChannelService(Path(self.tmp.name) / 'channels.db', channel_home=home)
            svc.close()
        self.assertFalse(old.exists())

    def test_extreme_aspect_ratio_pixel_cap_after_rounding(self):
        import math
        expression = media._scale_filter(MediaLimits(max_pixels=256))
        for iw, ih in ((1, 10000), (10000, 1), (10000, 10000)):
            parts = expression.split("scale=w='")[1:]
            for part in parts:
                wexpr, rest = part.split("':h=", 1)
                hexpr = rest.rstrip(",").strip("'")
                scope = dict(iw=iw, ih=ih, trunc=math.trunc, sqrt=math.sqrt, min=min, max=max)
                w = eval(wexpr, {'__builtins__': {}}, scope)
                h = eval(hexpr, {'__builtins__': {}}, scope)
                iw, ih = w, h
            self.assertGreaterEqual(iw, 2)
            self.assertGreaterEqual(ih, 2)
            self.assertLessEqual(iw, 1280)
            self.assertLessEqual(ih, 720)
            self.assertLessEqual(iw * ih, 256)
