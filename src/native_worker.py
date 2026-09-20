"""Isolated, version-pinned experiment with the installed Doubao speech SDK.

This is not a supported public API. No injection, binary patching, microphone,
keyboard, clipboard, focus, or IME RPC operations are used. Native logs remain
in the parent's memory; only selected diagnostic fields are saved.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parent
from compatibility import compatible, PEImage
PREFIX = '@@ASR_PROBE@@'
RECORDS = None



def emit(**fields):
    if RECORDS is not None:
        with RECORDS.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(fields, ensure_ascii=False) + '\n')



class NativeBuffer:
    def __init__(self, size):
        self.buf = C.create_string_buffer(size)
        self.refs = []

    def integer(self, offset, value):
        struct.pack_into('<i', self.buf, offset, value)

    def pointer(self, offset, value):
        if isinstance(value, str):
            value = value.encode('utf-8')
        if isinstance(value, bytes):
            value = C.create_string_buffer(value)
        self.refs.append(value)
        struct.pack_into('<Q', self.buf, offset, C.cast(value, C.c_void_p).value or 0)


def worker(wav_path, install):
    INSTALL = Path(install)
    if not compatible(INSTALL):
        raise RuntimeError('Unsupported component version')
    with wave.open(str(wav_path), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Probe requires 16 kHz mono PCM16 WAV.')
        pcm = audio.readframes(audio.getnframes())
    if len(pcm) > 16000 * 2 * 60:
        raise ValueError('This experiment is limited to one minute.')
    pe = PEImage(INSTALL / 'ImeService.exe')

    def static(rva):
        return pe.get_data(rva, 512).split(b'\0')[0]

    # Original installed application configuration stays in this process.
    url, frontier, business, app_config, resource = [static(rva) for rva in
        (0xff31d0, 0xff3208, 0xff3240, 0xff3250, 0xff3268)]
    version = static(0xff210c)
    retention = []
    handle = C.c_void_p()
    finished = threading.Event()
    events = []
    results = []
    errors = []

    with os.add_dll_directory(str(INSTALL)):
        runtime = C.WinDLL(str(INSTALL / 'DoubaoIme.Settings.NativeRuntime.dll'))
        sdk = C.WinDLL(str(INSTALL / 'audioeffect-mt.dll'))

        def bind(lib, name, args, result=C.c_int):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
            return fn

        init = bind(runtime, 'settings_runtime_init_applog', [])
        get_device = bind(runtime, 'settings_runtime_get_device_id', [C.c_void_p, C.c_int])
        shutdown = bind(runtime, 'settings_runtime_shutdown', [])
        context = bind(sdk, 'SAMICoreInitContext', [C.c_int, C.c_void_p])
        create = bind(sdk, 'SAMICoreCreateHandleByIdentify', [C.POINTER(C.c_void_p), C.c_int, C.c_void_p])
        process = bind(sdk, 'SAMICoreProcess', [C.c_void_p, C.c_void_p, C.c_void_p])
        prop = bind(sdk, 'SAMICoreSetProperty', [C.c_void_p, C.c_int, C.c_void_p])
        destroy = bind(sdk, 'SAMICoreDestroyHandle', [C.c_void_p])
        free_event = bind(sdk, 'SAMICoreDestroyAudioBlock', [C.c_void_p], None)
        init_ret = init()
        did_buffer = C.create_string_buffer(1024)
        for _ in range(20):
            get_device(did_buffer, len(did_buffer))
            if did_buffer.value:
                break
            time.sleep(.25)
        emit(stage='runtime', return_code=init_ret, device_ready=bool(did_buffer.value))
        if not did_buffer.value:
            shutdown()
            return
        did = did_buffer.value.decode()
        version_text = version.decode()
        metadata = json.dumps(dict(device_platform='windows', device_type='Windows',
            channel='release', app_name='ImeService', aid='685343', os='windows',
            device_id=did, version_name=version_text, app_version=version_text), separators=(',', ':'))
        identity = json.dumps(dict(device_id=did, aid='685343'), separators=(',', ':'))
        options = json.dumps(dict(s2a_send_enable=False, enable_text_filter=True,
            enable_print_chinese=False, enable_asr_twopass=True, enable_asr_threepass=True,
            use_twopass_retry=True, disable_user_words=False, did=did,
            app_name='ImeService', app_version=version_text), separators=(',', ':'))

        # Structures reconstructed from the matching installed executable.
        front = NativeBuffer(56)
        for off, value in [(0, frontier), (8, app_config), (16, metadata),
                           (32, did_buffer), (40, version)]:
            front.pointer(off, value)
        for off, value in [(24, 10), (28, 685343), (48, 3), (52, 5000)]:
            front.integer(off, value)
        ret = context(12, front.buf)
        emit(stage='frontier', return_code=ret)
        if ret:
            shutdown()
            return
        timeout = C.c_int(3000)
        context(14, C.byref(timeout))
        pool = (C.c_int * 3)(5, 5000, 500)
        ret = context(7, pool)
        emit(stage='pool', return_code=ret)
        if ret:
            shutdown()
            return
        connection = NativeBuffer(80)
        instruction = pe.get_data(0x714f16, 7)
        separator = static(0x714f16 + 7 + struct.unpack('<i', instruction[3:])[0])
        for off, value in [(0, business), (8, frontier + separator + url), (16, resource),
                           (24, identity), (40, app_config), (48, metadata)]:
            connection.pointer(off, value)
        for off, value in [(32, 5000), (56, 1), (60, 3), (64, 2), (68, 5000)]:
            connection.integer(off, value)
        ret = context(8, connection.buf)
        emit(stage='connection', return_code=ret)
        if ret:
            context(10, None)
            shutdown()
            return

        callback_type = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)

        def callback(kind):
            def receive(event, userdata):
                try:
                    if not event:
                        return
                    tag = C.c_int.from_address(event).value
                    data = C.c_void_p.from_address(event + 8).value
                    events.append({'kind': kind, 'tag': tag})
                    if tag == 600 and data:
                        code = C.c_int.from_address(data + 8).value
                        if kind == 'error':
                            errors.append({'code': code})
                        payload_ptr = C.c_void_p.from_address(data + 48).value
                        if payload_ptr:
                            payload = json.loads(C.string_at(payload_ptr).decode('utf-8'))
                            for item in payload.get('results', []):
                                if isinstance(item, dict) and isinstance(item.get('text'), str):
                                    results.append({key: item[key] for key in
                                        ('text', 'start_time', 'end_time', 'is_vad_finished', 'stream_asr_finish')
                                        if key in item})
                        if kind in ('finish', 'error'):
                            finished.set()
                    if tag == 601 and data:
                        events[-1]['network_state'] = C.c_int.from_address(data).value
                except Exception as exc:
                    errors.append({'callback_error': type(exc).__name__})
                finally:
                    if event:
                        free_event(event)
            return callback_type(receive)

        callbacks = NativeBuffer(64)
        for i, kind in enumerate(('start', 'error', 'finish', 'result', 'network')):
            fn = callback(kind)
            retention.append(fn)
            callbacks.pointer(i * 8, fn)
        params = NativeBuffer(240)
        headers = (C.c_int * 4).from_buffer_copy(pe.get_data(0xff4240, 16))
        for off, value in [(0, url), (8, resource), (16, app_config), (24, metadata),
                           (40, str(uuid.uuid4())), (64, identity), (72, b'speech_opus'),
                           (96, b'zh'), (104, options), (160, callbacks.buf), (168, business),
                           (192, headers), (232, b'')]:
            params.pointer(off, value)
        for off, value in [(56, 1), (80, 16000), (84, 1), (88, 5000), (92, 1),
                           (176, 1), (184, 1), (188, 7), (200, 4), (204, 0x251c),
                           (208, 6000), (212, 2000), (216, 40), (224, 1)]:
            params.integer(off, value)
        emit(stage='create_begin')
        ret = create(C.byref(handle), 0x29e, params.buf)
        emit(stage='create', return_code=ret, handle_ready=bool(handle.value))
        if ret or not handle.value:
            context(10, None)
            shutdown()
            return
        try:
            process_returns = set()
            sent_bytes = 0
            for offset in range(0, len(pcm), 1280):
                if errors:
                    break
                chunk = C.create_string_buffer(pcm[offset:offset + 1280])
                block = NativeBuffer(32)
                block.integer(4, len(chunk.raw) - 1)
                block.pointer(8, chunk)
                process_returns.add(process(handle, block.buf, None))
                sent_bytes += len(chunk.raw) - 1
                time.sleep(.04)
            emit(stage='audio_sent', bytes=sent_bytes, expected_bytes=len(pcm), return_codes=sorted(process_returns))
            stop_ret = prop(handle, 0xa2a, None)
            finished.wait(10)
            time.sleep(.5)
            emit(stage='recognition', stop_return=stop_ret, events=events, errors=errors,
                 results=results, finished=finished.is_set())
        finally:
            emit(stage='destroy', return_code=destroy(handle))
            context(10, None)
            # Original app releases its own frontier context with kind 13.
            context(13, None)
            shutdown()


def main():
    global RECORDS
    parser = argparse.ArgumentParser()
    parser.add_argument('wav', type=Path)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--records', type=Path, required=True)
    args = parser.parse_args()
    RECORDS = args.records
    try:
        worker(args.wav, args.install_dir)
        return 0
    except Exception as exc:
        emit(stage='exception', error_type=type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
