import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orchd.channels import media
from orchd.channels.media import MediaLimits, process_media


def fake_runner(duration=10.0, calls=None, timeout_on=None):
    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        if timeout_on and timeout_on in argv[0]:
            raise subprocess.TimeoutExpired(argv, timeout)
        if 'ffprobe' in argv[0]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'format': {'duration': str(duration)}}).encode())
        out = Path(argv[-1])
        if '%03d' in out.name:
            for i in (1, 2, 3):
                (out.parent / f'frame_{i:03d}.png').write_bytes(b'png')
        else:
            out.write_bytes(b'wav')
        return SimpleNamespace(returncode=0, stdout=b'')
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
