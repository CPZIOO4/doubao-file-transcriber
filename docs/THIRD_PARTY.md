# 第三方组件

| 组件 | 固定版本 | 用途 | 许可与来源 |
| --- | --- | --- | --- |
| CPython Windows x64 embeddable | 3.14.7 | 后台 Python 运行时 | [官方发行文件](https://www.python.org/ftp/python/3.14.7/)，许可随包保留为 `runtime/LICENSE.txt` |
| NAudio | 1.10.0 | Media Foundation 音频读取与重采样 | [NuGet](https://www.nuget.org/packages/NAudio/1.10.0)、[源码](https://github.com/naudio/NAudio/tree/v1.10.0)，[Ms-PL 许可](../licenses/NAudio-license.txt) |
| Inno Setup | 6.7.3 | 安装器构建 | [官方来源](https://jrsoftware.org/isdl.php)，[许可](../licenses/Inno-Setup-license.txt) |
| 豆包输入法原生组件 | 指纹匹配的 0.9.0.0 | 在线识别 | 用户自行安装；不包含在源码、安装包或自动构建下载中；不受本项目 MIT 许可覆盖 |

安装包分发上述 Python 运行时、NAudio DLL 和生成的安装器，以及对应许可。Windows Media Foundation 与 .NET Framework 由用户系统提供。

依赖来源和 SHA-256 记录在 `scripts/dependencies.json`。下载后必须通过校验才会解包或执行。构建脚本固定使用这些版本，不自动跟随最新版本。
