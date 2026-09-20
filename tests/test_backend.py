"""Offline behavior tests; never load or call the Doubao runtime."""
import array
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import compatibility
import doubao_transcribe as backend


class BackendTests(unittest.TestCase):
    def test_output_does_not_overwrite_and_keeps_unicode(self):
        with tempfile.TemporaryDirectory() as folder:
            first = backend.write_unique(Path(folder), '测试', '你好')
            second = backend.write_unique(Path(folder), '测试', '第二次')
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(encoding='utf-8-sig').strip(), '你好')
            self.assertEqual(second.read_text(encoding='utf-8-sig').strip(), '第二次')

    def test_overlap_ignores_punctuation(self):
        self.assertEqual(backend.overlap_join('这是一个测试。', '测试，继续。'), ('这是一个测试。继续。', True))
        self.assertEqual(backend.overlap_join('第一段。', '第二段。'), ('第一段。\n第二段。', False))

    def test_incomplete_session_is_rejected(self):
        stages = [dict(stage='create', handle_ready=True, return_code=0),
                  dict(stage='audio_sent', return_codes=[0], bytes=8, expected_bytes=10),
                  dict(stage='recognition', errors=[], finished=True, stop_return=0,
                       results=[dict(text='不能误报成功', is_vad_finished=True)])]
        with self.assertRaises(RuntimeError):
            backend.final_text(stages)
        stages[1]['bytes'] = 10
        self.assertEqual(backend.final_text(stages), '不能误报成功')
        stages[2]['finished'] = False
        with self.assertRaises(RuntimeError):
            backend.final_text(stages)

    def test_forced_segments_cover_all_samples_with_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(array.array('h', [1000]) .tobytes() * (83 * 16000))
            chunks = list(backend.segments(path))
            self.assertEqual(chunks[0][0], 0)
            self.assertEqual(chunks[-1][1], 83)
            for i, (start, end, pcm, overlap) in enumerate(chunks):
                self.assertLessEqual(end - start, 40.001)
                self.assertAlmostEqual(len(pcm) / 32000, end - start)
                self.assertEqual(overlap, i > 0)
                if i:
                    self.assertAlmostEqual(chunks[i - 1][1] - start, .8)

    def test_missing_and_unknown_components_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(compatibility.sys, 'platform', 'win32'), patch.object(compatibility.struct, 'calcsize', return_value=8):
                self.assertEqual(compatibility.check([root])['status'], 'missing')
                (root / 'ImeService.exe').write_bytes(b'not-a-supported-binary')
                self.assertEqual(compatibility.check([root])['status'], 'incompatible')

    def test_partial_result_and_cache_cleanup_after_network_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'placeholder')
            cache, output = root / 'cache', root / 'output'

            def fake_decode(_source, destination):
                with wave.open(str(destination), 'wb') as stream:
                    stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    stream.writeframes(b'\1\0' * 16000)

            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=fake_decode), \
                 patch.object(backend, 'segments', return_value=iter([(0, .5, b'\1\0' * 8000, False), (.5, 1, b'\1\0' * 8000, False)])), \
                 patch.object(backend, 'native_session', side_effect=[('已完成段落', []), RuntimeError('模拟网络失败')]), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '模拟网络失败'):
                    backend.transcribe(source, output, cache)
            outputs = list(output.glob('*.txt'))
            self.assertEqual(len(outputs), 1)
            self.assertIn('未完成', outputs[0].name)
            self.assertEqual(outputs[0].read_text(encoding='utf-8-sig').strip(), '已完成段落')
            self.assertEqual(list(cache.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
