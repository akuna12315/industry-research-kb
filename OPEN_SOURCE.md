# 研知开源许可与分发

Copyright (C) 2026 研知贡献者。

研知的原创程序代码（`scripts/`、`web/`、`packaging/` 中的原创代码及 `start-web.ps1`）按 **GNU General Public License 第 3 版或任何后续版本**发布，SPDX 标识为 `GPL-3.0-or-later`。

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

完整英文许可证见 [LICENSE](LICENSE)。本说明不修改许可证正文。

## 发布 EXE 和源码

- 在安装包的同一下载位置，无额外费用地提供该版本对应的可修改源码、构建及安装脚本、依赖版本与第三方许可。不能仅放许可证而不提供对应源码，也不能以混淆脚本或反编译结果代替原始源码。
- `python packaging/source_release.py` 生成 `dist/source/YanZhi-0.1.5-source.zip`、文件清单和 SHA256。白名单仅包括框架源码、测试、公开构建说明、依赖锁文件和许可；不导出本机知识库。保护版的原生编译兼容改写和前端混淆配置包含在可修改的构建脚本中。
- 普通版和代码保护版遵守相同的源码提供要求。用户可按 GPL 运行、研究、修改和再分发；不额外加入禁止商业使用、禁止修改或禁止再分发的条件。代码保护不用于限制这些权利。
- 对外提供二进制前，应核对源码与该二进制实际输入一致，并为随包的非系统第三方组件提供相应源码获取方式及所需源文件。源码导出工具只打包本项目，不会自动下载依赖源码；依赖锁文件和许可声明不能单独替代第三方对应源码。
- 通过下载发布时，将源码与二进制一起持续提供；若改用其他源码服务器，在二进制旁明确写出对应版本的下载地址，并保持可用。本项目没有设置“日后按需提供”的书面要约。

## 第三方与用户资料

第三方代码保留各自版权和许可，不把所有依赖改授 GPL。`packaging/ChineseSimplified.isl` 保留原译者声明；Python、依赖和构建工具声明由 `packaging/prepare_notices.py` 汇总到 `THIRD-PARTY-NOTICES.txt`。PyInstaller 使用其 GPL 及打包例外，保护版另保留 Nuitka 和 JavaScript Obfuscator 的许可。

本项目的 GPL 声明不自动改变用户导入的文件、行业资料、附件、数据库、模型配置、对话及第三方内容的权利。这些内容不属于公开框架源码，不随源码包或安装包发布。

本次许可声明日期：2026-11-05。此前已交付的 EXE 不会因修改文档自动加入许可文件；发布旧 EXE 时也须一并提供相应的许可证、声明及对应源码。

依据：[GPLv3 正文](https://www.gnu.org/licenses/gpl-3.0.html)第 1、4、5、6 条；[GNU 许可 FAQ](https://www.gnu.org/licenses/gpl-faq.en.html)。
