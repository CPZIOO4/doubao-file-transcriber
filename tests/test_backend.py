"""Offline behavior tests; never load or call the Doubao runtime."""
import array
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import compatibility
import doubao_transcribe as backend


class BackendTests(unittest.TestCase):
    @staticmethod
    def fake_decode(_source, destination):
        with wave.open(str(destination), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\1\0' * 16000)

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

    def test_balanced_plan_prefers_real_pauses_without_a_tiny_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'speech.wav'
            speech = b'\xe8\x03'
            quiet = b'\0\0'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(speech * (26 * 16000) + quiet * 16000 +
                                   speech * (26 * 16000) + quiet * 16000 +
                                   speech * (26 * 16000))
            plan = backend.segment_plan(path)
            self.assertEqual(len(plan), 3)
            lengths = [(end - start) / 16000 for start, end, _ in plan]
            self.assertLess(max(lengths) - min(lengths), 9)
            self.assertLessEqual(max(lengths), 40)
            self.assertTrue(all(not overlap for _, _, overlap in plan))
            self.assertTrue(26 <= plan[0][1] / 16000 <= 27)
            self.assertTrue(53 <= plan[1][1] / 16000 <= 54)

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

            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'segment_plan', return_value=[(0, 8000, False), (8000, 16000, False)]), \
                 patch.object(backend, 'native_session', side_effect=[('已完成段落', []), RuntimeError('模拟网络失败'), RuntimeError('模拟网络失败')]), \
                 patch.object(backend.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '模拟网络失败'):
                    backend.transcribe(source, output, cache, workers=1, checkpoint_dir=root / 'checkpoints')
            outputs = list(output.glob('*.txt'))
            self.assertEqual(len(outputs), 1)
            self.assertIn('未完成', outputs[0].name)
            self.assertIn('已完成段落\n【第 2 段待补转写', outputs[0].read_text(encoding='utf-8-sig'))
            self.assertEqual(list(cache.iterdir()), [])
            self.assertEqual(len(list((root / 'checkpoints').glob('*.json'))), 1)

    def test_worker_limit_and_out_of_order_completion(self):
        self.assertEqual(backend.worker_count(1), 1)
        self.assertEqual(backend.worker_count(20), 20)
        for invalid in (0, 21):
            with self.assertRaises(ValueError):
                backend.worker_count(invalid)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'placeholder')
            lock = threading.Lock()
            active = 0
            peak = 0
            completed = []
            paths = []

            def fake_session(wav_path, _temp, number, _duration, _install):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    paths.append(wav_path)
                self.assertTrue(wav_path.is_file())
                time.sleep(.12 if number == 1 else .03)
                with lock:
                    active -= 1
                    completed.append(number)
                return '第%d段' % number, []

            plan = [(i * 3200, (i + 1) * 3200, False) for i in range(5)]
            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'segment_plan', return_value=plan), \
                 patch.object(backend, 'native_session', side_effect=fake_session), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = backend.transcribe(source, root / 'output', root / 'cache', workers=3,
                                            checkpoint_dir=root / 'checkpoints')
            self.assertEqual(peak, 3)
            self.assertNotEqual(completed, sorted(completed))
            self.assertEqual(len(set(paths)), 5)
            self.assertTrue(all(not path.exists() for path in paths))
            self.assertEqual(Path(result['output']).read_text(encoding='utf-8-sig').strip(),
                             '\n'.join('第%d段' % i for i in range(1, 6)))

    def test_failure_keeps_only_ordered_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'placeholder')

            def fake_session(_wav_path, _temp, number, _duration, _install):
                if number == 3:
                    raise RuntimeError('模拟失败')
                return '第%d段' % number, []

            plan = [(i * 4000, (i + 1) * 4000, False) for i in range(4)]
            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'segment_plan', return_value=plan), \
                 patch.object(backend, 'native_session', side_effect=fake_session), \
                 patch.object(backend.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '第 3 段识别失败'):
                    backend.transcribe(source, root / 'output', root / 'cache', workers=4,
                                       checkpoint_dir=root / 'checkpoints')
            outputs = list((root / 'output').glob('*.txt'))
            self.assertEqual(len(outputs), 1)
            self.assertIn('未完成', outputs[0].name)
            self.assertIn('第1段\n第2段\n【第 3 段待补转写', outputs[0].read_text(encoding='utf-8-sig'))
            self.assertIn('第4段', outputs[0].read_text(encoding='utf-8-sig'))

    def test_locked_cache_does_not_turn_success_into_partial_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'placeholder')
            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'native_session', return_value=('完整结果', [])), \
                 patch.object(backend.shutil, 'rmtree', side_effect=PermissionError('locked')), \
                 patch.object(backend.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = backend.transcribe(source, root / 'output', root / 'cache', workers=1,
                                            checkpoint_dir=root / 'checkpoints')
            self.assertTrue(result['success'])
            self.assertTrue(result['warnings'][0].startswith('临时缓存未能清理'))
            self.assertEqual(Path(result['output']).read_text(encoding='utf-8-sig').strip(), '完整结果')
            self.assertFalse(list((root / 'output').glob('*未完成*')))

    def test_resume_only_missing_segment_and_fill_its_original_position(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'unchanged source')
            plan = [(i * 4000, (i + 1) * 4000, False) for i in range(4)]
            calls = []
            first_run = True

            def fake_session(_wav, _temp, number, _duration, _install):
                calls.append(number)
                if first_run and number == 2:
                    raise RuntimeError('network lost')
                return '第%d段' % number, []

            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'segment_plan', return_value=plan), \
                 patch.object(backend, 'native_session', side_effect=fake_session), \
                 patch.object(backend.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '第 2 段识别失败'):
                    backend.transcribe(source, root / 'output', root / 'cache', workers=4,
                                       checkpoint_dir=root / 'checkpoints')
                checkpoint = next((root / 'checkpoints').glob('*.json'))
                saved = json.loads(checkpoint.read_text(encoding='utf-8'))
                self.assertEqual(set(saved['texts']), {'1', '3', '4'})
                self.assertNotIn(str(source), checkpoint.read_text(encoding='utf-8'))
                partial = next((root / 'output').glob('*未完成*'))
                self.assertIn('第1段\n【第 2 段待补转写', partial.read_text(encoding='utf-8-sig'))
                self.assertIn('第3段\n第4段', partial.read_text(encoding='utf-8-sig'))
                first_run = False
                calls.clear()
                result = backend.transcribe(source, root / 'output', root / 'cache', workers=4,
                                            checkpoint_dir=root / 'checkpoints')
            self.assertEqual(calls, [2])
            self.assertEqual(result['resumed_segments'], 3)
            self.assertEqual(Path(result['output']).read_text(encoding='utf-8-sig').strip(),
                             '第1段\n第2段\n第3段\n第4段')
            self.assertFalse(list((root / 'checkpoints').glob('*.json')))

    def test_transient_network_error_retries_only_its_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'source')
            calls = []

            def fake_session(_wav, _temp, number, _duration, _install):
                calls.append(number)
                if len(calls) == 1:
                    raise RuntimeError('network lost')
                return '重试成功', []

            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'native_session', side_effect=fake_session), \
                 patch.object(backend.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = backend.transcribe(source, root / 'output', root / 'cache', workers=1,
                                            checkpoint_dir=root / 'checkpoints')
            self.assertEqual(calls, [1, 1])
            self.assertEqual(Path(result['output']).read_text(encoding='utf-8-sig').strip(), '重试成功')

    def test_changed_source_cannot_reuse_old_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.wav'
            source.write_bytes(b'original')
            plan = [(0, 8000, False), (8000, 16000, False)]
            calls = []
            failing = True

            def fake_session(_wav, _temp, number, _duration, _install):
                calls.append(number)
                if failing and number == 2:
                    raise RuntimeError('network lost')
                return '内容%d' % number, []

            with patch.object(backend, 'check', return_value={'status': 'ready', 'install_dir': 'unused'}), \
                 patch.object(backend, 'decode_audio', side_effect=self.fake_decode), \
                 patch.object(backend, 'segment_plan', return_value=plan), \
                 patch.object(backend, 'native_session', side_effect=fake_session), \
                 patch.object(backend.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    backend.transcribe(source, root / 'output', root / 'cache', workers=1,
                                       checkpoint_dir=root / 'checkpoints')
                source.write_bytes(b'changed')
                failing = False
                calls.clear()
                result = backend.transcribe(source, root / 'output', root / 'cache', workers=1,
                                            checkpoint_dir=root / 'checkpoints')
            self.assertEqual(calls, [1, 2])
            self.assertEqual(result['resumed_segments'], 0)

    def test_corrupt_checkpoint_is_rejected_instead_of_silently_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = [(0, 16000, False)]
            store = backend.CheckpointStore(root, 'a' * 64, plan)
            store.path.write_text('[]', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, '恢复记录与当前音频分段不一致'):
                backend.CheckpointStore(root, 'a' * 64, plan)


if __name__ == '__main__':
    unittest.main()
