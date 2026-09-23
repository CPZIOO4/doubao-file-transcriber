"""File-to-TXT backend. Uses only the installed, version-pinned Doubao SDK.

Stdout is JSON status for the desktop app. Native logs never go to disk.
Each speech session runs in a disposable child process with a deadline.
"""
import argparse
import array
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave

from compatibility import check

ROOT = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def status(kind, **fields):
    print(json.dumps(dict(type=kind, **fields), ensure_ascii=False), flush=True)


def decode_audio(source, destination):
    decoder = ROOT / 'AudioDecode.exe'
    if not decoder.is_file():
        raise RuntimeError('音频读取组件缺失，请重新安装本工具。')
    try:
        decoded = subprocess.run([str(decoder), str(source), str(destination)],
                                 capture_output=True, timeout=600, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError('音频读取超时。') from None
    if decoded.returncode == 3:
        raise RuntimeError('系统缺少 Windows Media Foundation。Windows N 版需安装媒体功能包。')
    if decoded.returncode or not destination.exists():
        raise RuntimeError('无法读取音频。请使用 WAV、MP3、M4A 等 Windows 支持的格式；部分格式需要系统解码器。')


SAMPLE_RATE = 16000
MAX_SEGMENT_SECONDS = 40
TARGET_SEGMENT_SECONDS = 30
SEARCH_SECONDS = 4
OVERLAP_SECONDS = .8
SPLIT_VERSION = 1


def quiet_cut(audio, target, radius):
    """Find the center of a pause near a balanced target, if one is credible."""
    rate = audio.getframerate()
    window = max(1, rate // 100)  # 10 ms energy windows.
    low = max(0, target - radius)
    high = min(audio.getnframes(), target + radius)
    low = low // window * window
    audio.setpos(low)
    samples = array.array('h', audio.readframes(high - low))
    energies = [math.isqrt(sum(value * value for value in samples[i:i + window]) // window)
                for i in range(0, len(samples) - window + 1, window)]
    if not energies:
        return None
    ranked = sorted(energies)
    quiet_level = ranked[len(ranked) // 5]
    speech_level = ranked[len(ranked) * 4 // 5]
    threshold = 150
    if speech_level >= max(quiet_level * 2, quiet_level + 120):
        threshold = max(threshold, min(quiet_level * 1.6,
                                       quiet_level + (speech_level - quiet_level) * .35))
    candidates = []
    run_start = None
    for index, energy in enumerate(energies + [float('inf')]):
        if energy < threshold:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            run_length = index - run_start
            if run_length >= 20:  # At least 200 ms, not a brief low-energy phoneme.
                run_low = low + run_start * window
                run_high = low + index * window
                cut = min(max(target, run_low), run_high)
                candidates.append((abs(cut - target), -run_length, cut))
            run_start = None
    return min(candidates)[2] if candidates else None


def segment_plan(wav_path):
    """Choose evenly distributed cuts, moving each only a few seconds to a pause."""
    with wave.open(str(wav_path), 'rb') as audio:
        rate = audio.getframerate()
        if (rate, audio.getnchannels(), audio.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise RuntimeError('解码音频格式不正确，请重新安装本工具。')
        total = audio.getnframes()
        if total == 0:
            return []
        count = 1 if total <= MAX_SEGMENT_SECONDS * rate else math.ceil(total / (TARGET_SEGMENT_SECONDS * rate))
        radius = SEARCH_SECONDS * rate
        cuts = []
        forced = []
        for index in range(1, count):
            target = round(index * total / count)
            selected = quiet_cut(audio, target, radius)
            cuts.append(selected if selected is not None else target)
            forced.append(selected is None)
        boundaries = [0] + cuts + [total]
        plan = []
        overlap_frames = round(OVERLAP_SECONDS * rate)
        for index in range(count):
            start = boundaries[index] - (overlap_frames if index and forced[index - 1] else 0)
            end = boundaries[index + 1]
            if end - start > MAX_SEGMENT_SECONDS * rate:
                raise RuntimeError('音频分段超过识别上限，请报告此问题。')
            plan.append((start, end, bool(index and forced[index - 1])))
        return plan


def segments(wav_path):
    """Yield plan segments without holding all PCM in memory."""
    plan = segment_plan(wav_path)
    with wave.open(str(wav_path), 'rb') as audio:
        for start, end, overlapping in plan:
            audio.setpos(start)
            yield start / SAMPLE_RATE, end / SAMPLE_RATE, audio.readframes(end - start), overlapping


def final_text(stages):
    creation = next((s for s in stages if s.get('stage') == 'create'), {})
    audio = next((s for s in stages if s.get('stage') == 'audio_sent'), {})
    result = next((s for s in stages if s.get('stage') == 'recognition'), {})
    if not creation.get('handle_ready') or creation.get('return_code') != 0:
        raise RuntimeError('豆包识别会话未能建立。请检查网络及已安装版本。')
    if audio.get('return_codes') != [0] or audio.get('bytes') != audio.get('expected_bytes'):
        raise RuntimeError('录音没有完整送入识别组件，未保存为成功结果。')
    if result.get('errors') or not result.get('finished') or result.get('stop_return') != 0:
        raise RuntimeError('豆包未正常结束识别。请检查网络后重试。')
    # Prefer the final full-utterance revision, not the unpunctuated alternative
    # or streaming prefixes. Never concatenate incremental hypotheses.
    finals = [r for r in result.get('results', []) if r.get('is_vad_finished') and r.get('text')]
    if not finals:
        raise RuntimeError('豆包没有返回最终文字（可能是静音或识别未完成）。')
    return finals[-1]['text'].strip()


def overlap_join(previous, incoming):
    def letters(text):
        return [(i, c.casefold()) for i, c in enumerate(text)
                if not c.isspace() and not unicodedata.category(c).startswith('P')]
    left, right = letters(previous), letters(incoming)
    for size in range(min(16, len(left), len(right)), 1, -1):
        if [c for _, c in left[-size:]] == [c for _, c in right[:size]]:
            rest = incoming[right[size-1][0]+1:]
            # Keep the punctuation already present at the join, avoiding "。，".
            if previous and unicodedata.category(previous[-1]).startswith('P'):
                rest = rest.lstrip()
                while rest and unicodedata.category(rest[0]).startswith('P'):
                    rest = rest[1:].lstrip()
            return previous + rest, True
    return previous + '\n' + incoming, False


def native_session(wav_path, temp, number, duration, install):
    records = temp / ('session-%04d.jsonl' % number)
    records.unlink(missing_ok=True)  # A retry must not read records from its earlier attempt.
    command = [sys.executable, '-I', '-B', str(ROOT / 'native_worker.py'), str(wav_path),
               '--install-dir', str(install), '--records', str(records)]
    try:
        child = subprocess.run(command, capture_output=True,
                               timeout=duration + 35, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError('识别超时；该次后台进程已结束，可以重试。') from None
    stages = [json.loads(line) for line in records.read_text(encoding='utf-8').splitlines()] if records.exists() else []
    if child.returncode:
        raise RuntimeError('豆包后台组件退出（代码 %s）。未修改正在使用的输入法。' % child.returncode)
    return final_text(stages), stages


def write_unique(folder, stem, text, partial=False):
    folder.mkdir(parents=True, exist_ok=True)
    suffix = '.豆包转写' + ('.未完成' if partial else '')
    for number in range(10000):
        path = folder / (stem + suffix + ('' if number == 0 else ' (%d)' % number) + '.txt')
        try:
            with path.open('x', encoding='utf-8-sig', newline='') as stream:
                stream.write(text + '\r\n')
            return path
        except FileExistsError:
            continue
    raise RuntimeError('同名输出过多，请更换输出目录。')


def worker_count(value):
    number = int(value)
    if not 1 <= number <= 20:
        raise ValueError('并发任务池数量必须在 1 到 20 之间。')
    return number


def source_hash(source):
    digest = hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class CheckpointStore:
    """Persist verified segment text, never audio, outside the disposable task cache."""

    def __init__(self, root, source_digest, plan):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / ('checkpoint-%s-v%d.json' % (source_digest, SPLIT_VERSION))
        self.temp_path = self.path.with_suffix('.tmp')
        self.temp_path.unlink(missing_ok=True)
        self.plan = [[start, end, overlap] for start, end, overlap in plan]
        self.payload = {'version': SPLIT_VERSION, 'source_sha256': source_digest,
                        'plan': self.plan, 'texts': {}}
        if self.path.exists():
            try:
                restored = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise RuntimeError('恢复记录无法读取：%s。请清理该文件后重试。' % self.path) from exc
            if (not isinstance(restored, dict) or
                    restored.get('version') != SPLIT_VERSION or
                    restored.get('source_sha256') != source_digest or
                    restored.get('plan') != self.plan or
                    not isinstance(restored.get('texts'), dict)):
                raise RuntimeError('恢复记录与当前音频分段不一致：%s。请清理该文件后重试。' % self.path)
            for key, value in restored['texts'].items():
                if not key.isdecimal() or not 1 <= int(key) <= len(plan) or not isinstance(value, str):
                    raise RuntimeError('恢复记录内容无效：%s。请清理该文件后重试。' % self.path)
            self.payload = restored

    def get(self, number):
        return self.payload['texts'].get(str(number))

    def completed(self):
        return {int(number): value for number, value in self.payload['texts'].items()}

    def save(self, number, value):
        key = str(number)
        existed = key in self.payload['texts']
        previous = self.payload['texts'].get(key)
        self.payload['texts'][key] = value
        try:
            with self.temp_path.open('w', encoding='utf-8', newline='') as stream:
                json.dump(self.payload, stream, ensure_ascii=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.temp_path, self.path)
        except Exception:
            if existed:
                self.payload['texts'][key] = previous
            else:
                self.payload['texts'].pop(key, None)
            try:
                self.temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def clear(self, warnings):
        for attempt in range(5):
            try:
                self.path.unlink(missing_ok=True)
                return
            except OSError:
                if attempt < 4:
                    time.sleep(.3)
        warnings.append('临时缓存未能清理：' + str(self.path))


def recognize_with_retry(wav_path, temp, number, duration, install):
    for attempt in range(2):
        try:
            return native_session(wav_path, temp, number, duration, install)
        except RuntimeError:
            if attempt:
                raise
            time.sleep(2)


def compose_transcript(plan, texts, partial, warnings):
    merged = ''
    previous_text_number = None
    for number, (start, end, overlapping) in enumerate(plan, 1):
        if number not in texts:
            if partial:
                marker = '【第 %d 段待补转写：%.1f–%.1f 秒】' % (number, start / SAMPLE_RATE, end / SAMPLE_RATE)
                merged += ('\n' if merged else '') + marker
            previous_text_number = None
            continue
        incoming = texts[number]
        if not incoming:
            previous_text_number = None
            continue
        if overlapping and previous_text_number == number - 1:
            merged, matched = overlap_join(merged, incoming)
            if not matched:
                warnings.append('连续长句的分段处未能自动去重，请校对。')
        else:
            merged += ('\n' if merged else '') + incoming
        previous_text_number = number
    return merged


@contextmanager
def task_directory(root, warnings):
    path = Path(tempfile.mkdtemp(prefix='audio-', dir=root))
    try:
        yield path
    finally:
        for attempt in range(5):
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt < 4:
                    time.sleep(.3)
        else:
            warnings.append('临时缓存未能清理：' + str(path))


def transcribe(source, output_dir, work_dir=None, workers=5, checkpoint_dir=None):
    workers = worker_count(workers)
    if not source.is_file():
        raise RuntimeError('录音文件不存在。')
    compatibility = check()
    if compatibility['status'] != 'ready':
        raise RuntimeError(compatibility['message'])
    install = compatibility['install_dir']
    app_root = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'DoubaoFileTranscriber'
    temp_root = Path(work_dir) if work_dir else app_root / 'Cache'
    checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else app_root / 'Checkpoints'
    temp_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {'input': str(source), 'segments': [], 'warnings': []}
    plan = []
    results = {}
    try:
        with task_directory(temp_root, report['warnings']) as temp:
            decoded = temp / 'decoded.wav'
            status('progress', percent=0, message='正在读取录音')
            original_stat = source.stat()
            decode_audio(source, decoded)
            digest = source_hash(source)
            current_stat = source.stat()
            if ((original_stat.st_size, original_stat.st_mtime_ns) !=
                    (current_stat.st_size, current_stat.st_mtime_ns)):
                raise RuntimeError('录音文件在处理期间发生变化，请保存完毕后重试。')
            with wave.open(str(decoded), 'rb') as audio:
                duration = audio.getnframes() / SAMPLE_RATE
            if duration <= .05:
                raise RuntimeError('录音过短或为空。')
            report['duration_seconds'] = duration
            plan = segment_plan(decoded)
            checkpoint = CheckpointStore(checkpoint_root, digest, plan)
            results = checkpoint.completed()
            resumed = len(results)
            if resumed:
                status('progress', percent=round(sum((end - start) for number, (start, end, _)
                                                     in enumerate(plan, 1) if number in results) / (duration * SAMPLE_RATE) * 95),
                       message='已恢复 %d / %d 段，继续补齐缺失段' % (resumed, len(plan)))
            pending = {}
            errors = {}
            finished = resumed
            processed_frames = sum(end - start for number, (start, end, _) in enumerate(plan, 1)
                                   if number in results)
            next_number = 1

            with wave.open(str(decoded), 'rb') as audio, ThreadPoolExecutor(max_workers=workers) as pool:
                def submit_next():
                    nonlocal next_number
                    while len(pending) < workers and next_number <= len(plan):
                        number = next_number
                        next_number += 1
                        if number in results:
                            continue
                        start, end, overlapping = plan[number - 1]
                        audio.setpos(start)
                        pcm = audio.readframes(end - start)
                        if len(pcm) != (end - start) * 2:
                            raise RuntimeError('分段读取不完整，请重试。')
                        if not any(pcm):
                            checkpoint.save(number, '')
                            results[number] = ''
                            continue
                        wav_path = temp / ('chunk-%04d.wav' % number)
                        with wave.open(str(wav_path), 'wb') as chunk:
                            chunk.setnchannels(1)
                            chunk.setsampwidth(2)
                            chunk.setframerate(SAMPLE_RATE)
                            chunk.writeframes(pcm)
                        future = pool.submit(recognize_with_retry, wav_path, temp, number,
                                             (end - start) / SAMPLE_RATE, install)
                        pending[future] = (number, wav_path)

                submit_next()
                while pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        number, wav_path = pending.pop(future)
                        try:
                            segment_text, _ = future.result()
                            checkpoint.save(number, segment_text)
                            results[number] = segment_text
                        except Exception as exc:
                            errors[number] = exc
                        finally:
                            try:
                                wav_path.unlink(missing_ok=True)
                            except OSError:
                                pass  # The task directory gets a second cleanup attempt.
                        finished += 1
                        start, end, _ = plan[number - 1]
                        processed_frames += end - start
                    status('progress', percent=min(95, round(processed_frames / (duration * SAMPLE_RATE) * 95)),
                           message='已完成 %d / %d 段，进行中 %d 段' % (finished, len(plan), len(pending)))
                    if not errors:
                        submit_next()

            if errors:
                number = min(errors)
                raise RuntimeError('第 %d 段识别失败：%s。已保存成功段，再次加入同一文件可补齐。' %
                                   (number, errors[number]))

        text = compose_transcript(plan, results, partial=False, warnings=report['warnings'])
        if not text.strip():
            checkpoint.clear(report['warnings'])
            raise RuntimeError('没有识别到语音。')
        target = write_unique(output_dir, source.stem, text)
        checkpoint.clear(report['warnings'])
        report['segments'] = [dict(start=start / SAMPLE_RATE, end=end / SAMPLE_RATE,
                                   overlap=overlapping, text=results[number])
                              for number, (start, end, overlapping) in enumerate(plan, 1)
                              if results.get(number)]
        report.update(output=str(target), elapsed_seconds=round(time.monotonic()-started, 2),
                      success=True, resumed_segments=resumed)
        cache_warning = next((item for item in report['warnings'] if item.startswith('临时缓存')), None)
        status('done', percent=100, output=str(target), message='完成',
               warnings=report['warnings'], cache_warning=cache_warning)
        return report
    except Exception:
        if plan and any(results.values()):
            partial_text = compose_transcript(plan, results, partial=True, warnings=report['warnings'])
            partial = write_unique(output_dir, source.stem, partial_text, partial=True)
            status('partial', output=str(partial), message='已保留完成的分段；再次加入同一文件可补齐缺失段')
        raise


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path, nargs='?')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--checkpoint-dir', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--workers', type=worker_count, default=5)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(), ensure_ascii=False), flush=True)
        return 0
    try:
        if args.input is None:
            raise RuntimeError('请选择录音文件。')
        source = args.input.resolve()
        transcribe(source, args.output_dir or Path.home() / 'Documents' / '豆包转写结果',
                   args.work_dir, args.workers, args.checkpoint_dir)
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else '处理失败：' + type(exc).__name__
        status('error', message=message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
