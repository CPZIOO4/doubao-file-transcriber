# 从源码构建

在 Windows x64 上构建。需要系统自带的 .NET Framework 4.7.2+（包括 x64 C# 编译器）和 PowerShell 5.1+。PowerShell 7 也可用。

```powershell
git clone https://github.com/CPZIOO4/doubao-file-transcriber.git
cd doubao-file-transcriber
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

脚本从官方 Python、NuGet 和 Inno Setup GitHub Release 下载固定版本并检查 SHA-256；下载物存放在 `.deps`。如果没有通过参数提供 Inno 编译器，脚本会把 Inno Setup 6.7.3 以当前用户方式安装到 `.deps/inno-6.7.3`。该工具安装步骤可能在 Windows 中注册卸载项。构建前请阅读其[许可](../licenses/Inno-Setup-license.txt)。

不希望脚本安装编译器时，可使用自己已安装的 Inno Setup 6.7.3：

```powershell
.\scripts\build.ps1 -InnoCompiler 'C:\path\to\ISCC.exe'
```

只构建免安装目录并运行离线测试：

```powershell
.\scripts\build.ps1 -SkipInstaller
```

构建不需要安装豆包，也不会自动下载、启动豆包或发送音频。每次在 `.build/package-<随机编号>` 生成干净的运行目录，路径写入 `.build/last-package.txt`。安装包及校验值生成在 `dist`。所有产物、依赖和测试输出均被 Git 忽略。

Python `_pth` 只包含官方标准库、运行时目录和 `../app`，不启用用户 site-packages。构建时复制原发行包许可证；不用 pip，不依赖系统 Python。

## 离线测试

构建脚本自动运行 `tests` 中的 unittest。也可用已安装的 Python 3.10+ 执行：

```powershell
python -B -m unittest discover -s tests -v
```

这些测试验证均匀分段、停顿切点、并发数量边界、乱序完成后按序拼接、单段重试、恢复缓存与缺口原位补齐、输入变化时不误用旧文字、缓存清理、会话错误检查、不覆盖输出及兼容性拒绝路径，不调用豆包服务。GitHub Actions 使用 Windows 环境构建并运行离线测试，不具备在线识别或第二台物理电脑验收的含义。

## 在线验证与发布

只在自行安装兼容版本且可联网的 Windows 电脑上，用非敏感的合成或可公开使用的音频做小样本测试。检查文字、缓存清理、输入输出设备、取消及错误提示；在 `docs/VALIDATION.md` 记录实际覆盖范围。

发布时只上传 `dist` 中安装包与校验文件；不提交 `.build`、`.deps`、原生运行时日志、录音或转写结果。构建路径和编译器元数据可能使独立构建的安装包哈希不同，本流程不声称字节级可复现。
