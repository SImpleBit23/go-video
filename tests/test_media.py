"""Actual MP4/AAC/H.264 demux on Windows FFmpeg. No car or internet."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SVC = ROOT / 'native/st-govideo-d'
sys.path.insert(0, str(SVC))
import vkfetch


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ffmpeg = os.environ.get('GO_VIDEO_TEST_FFMPEG') or str(ROOT.parent / 'video-tools/ffmpeg.exe')
        self.events = []
        self.emit = vkfetch.base.emit
        vkfetch.base.emit = lambda **event: self.events.append(event)
        # Windows has no pwrite; the target MGU is Linux. This shim only adapts the test host's file API.
        self.open=os.open
        if os.name=='nt': os.open=lambda file,flags,*args,**kw:self.open(file,flags|os.O_BINARY,*args,**kw)
        self.pwrite = getattr(os, 'pwrite', None)
        if not self.pwrite:
            def pwrite(fd, value, offset):
                old = os.lseek(fd, 0, os.SEEK_CUR)
                os.lseek(fd, offset, os.SEEK_SET)
                n = os.write(fd, value)
                os.lseek(fd, old, os.SEEK_SET)
                return n
            os.pwrite = pwrite

    def tearDown(self):
        vkfetch.base.emit = self.emit
        if not self.pwrite:
            del os.pwrite
        os.open=self.open
        self.tmp.cleanup()

    def test_combined_mp4_prepares_real_bmw_audio_and_video(self):
        source = self.dir / 'combined.mp4'
        subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2', '-pix_fmt', 'yuv420p',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25', '-c:a', 'aac', '-y', str(source)], check=True)
        vkfetch.prepare_media(source, self.dir, dict(id='-123_456', title='Go Video test', channel='Fixture', duration_s=2), self.ffmpeg)
        frames = vkfetch.base.validate(str(self.dir / 'index.bin'), str(self.dir / 'video.h264'))
        self.assertEqual(frames, 50)
        self.assertGreater((self.dir / 'audio.m4a').stat().st_size, 1000)
        subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-i', str(self.dir / 'audio.m4a'), '-f', 'null', '-'], check=True, capture_output=True)
        self.assertTrue(any(e['ev'] == 'audio_ready' for e in self.events))
        self.assertTrue(any(e['ev'] == 'video' and e['ms'] >= 1900 for e in self.events))
        self.assertEqual(self.events[-1], dict(ev='done'))
        self.assertFalse(vkfetch.CHILDREN)

    def test_faststart_mp4_yields_audio_head_before_input_finishes(self):
        source = self.dir / 'combined.mp4'
        subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi',
                        '-i', 'testsrc2=size=640x360:rate=25', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:sample_rate=48000', '-t', '25',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25',
                        '-c:a', 'aac', '-movflags', '+faststart', '-y', str(source)], check=True)
        class SlowSource:
            eof = False

            def read(self, size):
                time.sleep(0.01)
                data = stream.read(min(size, 16384))
                if not data:
                    self.eof = True
                return data

        with source.open('rb') as stream:
            slow = SlowSource()
            def emit(**event):
                self.events.append((event, slow.eof))
            vkfetch.base.emit = emit
            prefix = vkfetch.read_mp4_prefix(slow)
            self.assertIsNotNone(prefix)
            self.assertTrue(vkfetch.prepare_stream(slow, self.dir, dict(
                title='Go Video test', channel='Fixture', duration_s=25), self.ffmpeg, prefix=prefix))
        self.assertTrue(any(e['ev'] == 'audio_head' and not eof for e, eof in self.events))
        self.assertEqual(self.events[-1][0], dict(ev='done'))
        self.assertEqual(vkfetch.base.validate(str(self.dir / 'index.bin'), str(self.dir / 'video.h264')), 625)
        for name in ('audio.head.m4a', 'audio.m4a'):
            subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-i', str(self.dir / name),
                            '-f', 'null', '-'], check=True, capture_output=True)

    def test_tail_moov_is_rejected_without_reading_media_payload(self):
        source = io.BytesIO(b'\x00\x00\x00\x08ftyp\x00\x01\x00\x00mdat' + b'x' * 65536)
        self.assertIsNone(vkfetch.read_mp4_prefix(source))
        self.assertLessEqual(source.tell(), 24)

    def test_private_paths_and_protected_live_content_are_rejected(self):
        self.assertIsNone(vkfetch.VID.fullmatch('../etc/passwd'))
        self.assertIsNone(vkfetch.VID.fullmatch('-123_456/child'))
        self.assertIsNotNone(vkfetch.VID.fullmatch('-123_456'))
        with self.assertRaises(ValueError): vkfetch.metadata(dict(is_live=True), '-123_456')
        with self.assertRaises(ValueError): vkfetch.metadata(dict(has_drm=True), '-123_456')


if __name__ == '__main__':
    unittest.main(verbosity=2)
