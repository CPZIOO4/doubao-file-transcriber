"""File-to-TXT backend. Uses only the installed, version-pinned Doubao SDK.

Stdout is JSON status for the desktop app. Native logs never go to disk.
Each speech session runs in a disposable child process with a deadline.
"""
import argparse
import array
import json
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


def segments(wav_path):
    """Prefer quiet cuts in 25–40s. Overlap only forced continuous-speech cuts."""
    with wave.open(str(wav_path), 'rb') as audio:
        rate = audio.getframerate()
        total = audio.getnframes()
        start = 0
        overlap = False
        while start < total:
            end = min(start + 40 * rate, total)
            audio.setpos(start)
            pcm = audio.readframes(end - start)
            forced = False
            if end < total:
                samples = array.array('h', pcm)
                window = 160
                quiet = [sum(v*v for v in samples[i:i+window]) < 150*150*window
                         for i in range(0, len(samples)-window+1, window)]
                choices = []
                run = 0
                for i, is_quiet in enumerate(quiet):
                    run = run + 1 if is_quiet else 0
                    if run >= 25 and i >= 2500:
                        choices.append(i - 12)
                if choices:
                    cut = min(choices, key=lambda i: abs(i - 3300)) * window
                    end = start + cut
                    pcm = pcm[:cut * 2]
                else:
                    forced = True
            yield start / rate, end / rate, pcm, overlap
            if end == total:
                break
            start = end - (int(.8 * rate) if forced else 0)
            overlap = forced


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


def transcribe(source, output_dir, work_dir=None):
    if not source.is_file():
        raise RuntimeError('录音文件不存在。')
    compatibility = check()
    if compatibility['status'] != 'ready':
        raise RuntimeError(compatibility['message'])
    install = compatibility['install_dir']
    temp_root = Path(work_dir) if work_dir else Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'DoubaoFileTranscriber' / 'Cache'
    temp_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    text = ''
    report = {'input': str(source), 'segments': [], 'warnings': []}
    try:
        with tempfile.TemporaryDirectory(prefix='audio-', dir=temp_root) as temp_name:
            temp = Path(temp_name)
            decoded = temp / 'decoded.wav'
            status('progress', percent=0, message='正在读取录音')
            decode_audio(source, decoded)
            with wave.open(str(decoded), 'rb') as audio:
                duration = audio.getnframes() / 16000
            if duration <= .05:
                raise RuntimeError('录音过短或为空。')
            report['duration_seconds'] = duration
            for number, (start, end, pcm, overlapping) in enumerate(segments(decoded), 1):
                status('progress', percent=round(start / duration * 95),
                       message='正在识别第 %d 段（%.0f / %.0f 秒）' % (number, end, duration))
                wav_path = temp / 'chunk.wav'
                with wave.open(str(wav_path), 'wb') as chunk:
                    chunk.setnchannels(1)
                    chunk.setsampwidth(2)
                    chunk.setframerate(16000)
                    chunk.writeframes(pcm)
                if not any(pcm):
                    continue
                segment_text, stages = native_session(wav_path, temp, number, end-start, install)
                report['segments'].append({'start': start, 'end': end, 'overlap': overlapping,
                                           'text': segment_text})
                if overlapping and text:
                    text, matched = overlap_join(text, segment_text)
                    if not matched:
                        report['warnings'].append('连续长句的分段处未能自动去重，请校对。')
                else:
                    text += ('\n' if text else '') + segment_text
                status('progress', percent=round(end / duration * 95), message='第 %d 段已完成' % number)
        if not text.strip():
            raise RuntimeError('没有识别到语音。')
        target = write_unique(output_dir, source.stem, text)
        report.update(output=str(target), elapsed_seconds=round(time.monotonic()-started, 2), success=True)
        status('done', percent=100, output=str(target), message='完成', warnings=report['warnings'])
        return report
    except Exception:
        if text:
            partial = write_unique(output_dir, source.stem, text, partial=True)
            status('partial', output=str(partial), message='已保留完成的部分；完整录音尚未转写成功')
        raise


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path, nargs='?')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(), ensure_ascii=False), flush=True)
        return 0
    try:
        if args.input is None:
            raise RuntimeError('请选择录音文件。')
        source = args.input.resolve()
        transcribe(source, args.output_dir or Path.home() / 'Documents' / '豆包转写结果', args.work_dir)
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else '处理失败：' + type(exc).__name__
        status('error', message=message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
