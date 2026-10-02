# 文档翻译 · macOS 0.1.8

首个公开的 macOS 原生预发布版，构建号 **10**。本 fork 基于 [ilysom0611/doc-translator](https://github.com/ilysom0611/doc-translator)，保留原 Web 版并增加 SwiftUI 应用与内置文档引擎。macOS 应用版本与上游 Web 引擎版本分别维护。

## 下载与使用

下载 **DocTranslator-0.1.8-macos-arm64.zip**，解压后将 `DocTranslator.app` 放入「应用程序」目录。应用内置 Python 和所需依赖，无需虚拟环境、终端配置或额外应用密码。

打开「API 设置」，保存兼容 OpenAI Chat Completions 的服务地址、模型和 API Key，测试连接后导入文档。详细步骤见附件 `README-zh-CN.md` 或[中文使用说明](https://github.com/JiGuangYa/doc-translator/blob/macos-v0.1.8/macos/使用说明.md)。

**适用范围：**Apple Silicon，声明最低 macOS 14；实际验证系统为开发者当前 Mac 的 macOS 27.2。本构建采用本机签名，尚未进行 Developer ID 公证，其他 Mac 可能被 Gatekeeper 拦截；未验证跨机器分发或 Intel Mac。

## 已实现

- PDF、DOCX、PPTX、XLSX 的多文件导入、翻译、双栏阅读、修订和同格式导出。
- 保存 API 配置及默认语言，连接测试，导入与翻译进度，取消及继续翻译；续翻保持原任务的模型和语言。
- 持久化修订草稿、整批保存、导出前选择是否应用草稿；写入失败保留原文件和编辑内容。
- 原文与译文共享段落滚动；页面预览缩放、页码跳转、独立页数和内容版本缓存刷新。
- 搜索、状态筛选、原生方向键选择、归档恢复，以及跨文档和重启后的阅读位置恢复。
- 每次启动使用随机凭据和本机动态端口连接内置引擎；退出保存翻译进度并清理所属进程。
- 数据存入 `~/Library/Application Support/DocTranslatorMac/`，桌面版不自动清理旧任务；API Key 优先存入 macOS 钥匙串。
- 修复快速筛选列表的重入警告；发布前更新 Pillow 12.3.0 与 cryptography 50.0.2，并重新打包第三方许可证。

## 验证

- 后端及打包回归：**168 项通过、4 项因缺少上游外部样例跳过**；原生状态回归：**46 项通过**。
- 本地模拟 API 验证四种格式的导入、翻译、单段与整批修订、导出重开、预览、归档、取消续翻、退出重启及密钥轮换。
- 独立目录运行签名应用，验证 Finder 文件打开事件、搜索与键盘输入、修订导出、阅读恢复、API 错误和退出后无遗留测试引擎。
- 附带 `release-manifest.json` 和 `SHA256SUMS`，记录构建版本、对应源码提交、验证范围与文件校验值。

上述流程测试使用本地模拟 API。**真实服务连通性与模型翻译质量需在应用内配置后验证。**

## 已知限制

- 扫描 PDF 需要先做 OCR。复杂公式、PDF 排版、Word 混合样式、PPT 图表/SmartArt、Excel 图表及图片可能无法完全保真。
- PDF 译文放不下时保留该位置原文并显示排版提示，可缩短译文后重新保存。
- Office 页面预览需要本机安装 LibreOffice；缺失时仍可使用段落阅读、翻译与导出。
- 尚未进行 App Store 上架、公证或跨机器发行验证。

源码与构建方式见 [macos/README.md](https://github.com/JiGuangYa/doc-translator/blob/macos-v0.1.8/macos/README.md)。应用资源目录包含项目和第三方许可证。
