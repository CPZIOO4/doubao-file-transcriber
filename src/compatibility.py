"""Discover installed Doubao versions without reading accounts or user settings."""
import hashlib
import os
from pathlib import Path
import platform
import re
import struct
import sys

EXPECTED = {
    'ImeService.exe': '94b17bdca571ac3cd2dafa687b4cb8e3a789cda9d044276483003b0a9e8f77a6',
    'audioeffect-mt.dll': '16533a79fe707d5a217bba89586b3a9f6c812c759aa32261d0da2fe548468ea7',
    'DoubaoIme.Settings.NativeRuntime.dll': 'fd01727cf1d84e2ae0da23cc51464dc24be9f6024bc0812997e8979a3d6135a7',
}
OFFICIAL_URL = 'https://ime.doubao.com/'


def install_roots():
    import winreg
    roots = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                    0, winreg.KEY_READ | view) as uninstall:
                    for index in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as entry:
                                name = str(winreg.QueryValueEx(entry, 'DisplayName')[0])
                                if not ('豆包输入法' in name or 'doubaoime' in name.lower().replace(' ', '')):
                                    continue
                                for field in ('InstallLocation', 'UninstallString', 'DisplayIcon'):
                                    try:
                                        value = os.path.expandvars(str(winreg.QueryValueEx(entry, field)[0]))
                                    except OSError:
                                        continue
                                    if field == 'InstallLocation':
                                        roots.add(Path(value.strip('"')))
                                    else:
                                        match = re.match(r'^"([^"]+)"|^(.+?\.exe)', value, re.I)
                                        if match:
                                            roots.add(Path(match.group(1) or match.group(2)).parent)
                        except OSError:
                            continue
            except OSError:
                continue
    for variable in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if base:
            roots.add(Path(base) / 'DoubaoIME')
            if variable == 'LOCALAPPDATA':
                roots.add(Path(base) / 'Programs' / 'DoubaoIME')
    return roots


def candidate_directories(roots):
    candidates = set()
    for root in roots:
        root = Path(root)
        if (root / 'ImeService.exe').is_file():
            candidates.add(root)
        try:
            for child in (root / 'versions').iterdir():
                if child.is_dir() and (child / 'ImeService.exe').is_file():
                    candidates.add(child)
        except OSError:
            pass
    return sorted(candidates, key=lambda p: tuple(int(x) for x in re.findall(r'\d+', p.name)), reverse=True)


def compatible(directory):
    try:
        return all(hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected
                   for name, expected in EXPECTED.items())
    except OSError:
        return False


def check(roots=None):
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        return dict(status='unsupported_system', message='需要 Windows 10/11 的 64 位系统。')
    candidates = candidate_directories(install_roots() if roots is None else roots)
    if not candidates:
        return dict(status='missing', message='未找到豆包输入法。请从官网安装 Windows 版，启动并完成首次设置后，点击重新检测。', url=OFFICIAL_URL)
    for directory in candidates:
        if compatible(directory):
            return dict(status='ready', message='已找到兼容的豆包输入法，可以直接拖入录音。',
                        version='0.9.0.0', install_dir=str(directory))
    return dict(status='incompatible', message='已安装豆包输入法，但当前组件版本尚未适配。本工具目前验证支持 0.9.0.0 的指定组件，请更新转写工具；无需卸载正常使用的输入法。', url=OFFICIAL_URL)


class PEImage:
    """Minimal read-only RVA mapping for the fingerprinted installed executable."""
    def __init__(self, path):
        self.data = Path(path).read_bytes()
        header = struct.unpack_from('<I', self.data, 0x3c)[0]
        if self.data[header:header+4] != b'PE\0\0':
            raise ValueError('Invalid PE image')
        sections = struct.unpack_from('<H', self.data, header+6)[0]
        optional_size = struct.unpack_from('<H', self.data, header+20)[0]
        table = header + 24 + optional_size
        self.sections = []
        for index in range(sections):
            offset = table + index * 40
            size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', self.data, offset+8)
            self.sections.append((rva, raw_size, raw_offset))

    def get_data(self, rva, size):
        for start, length, offset in self.sections:
            if start <= rva and rva + size <= start + length:
                return self.data[offset+rva-start:offset+rva-start+size]
        raise ValueError('RVA outside a file section')
