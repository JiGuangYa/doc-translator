# 文档翻译预览版 0.3.0

Apple Silicon · macOS 14+ · SwiftUI + bundled Python core · optional GenOffice renderer.

| Item | Preview |
|---|---|
| App | 文档翻译预览版.app |
| Bundle ID | com.jiguang.doctranslator.preview |
| Library | ~/Library/Application Support/DocTranslatorUnifiedPreview |
| Provider Keychain service | com.jiguang.doctranslator.preview.providers |
| App unlock service | com.jiguang.doctranslator.preview.unlock |

## 使用

1. 首次打开设置独立的应用密码；之后可以使用系统解锁。
2. 在模型设置中填写地址、模型和密钥，支持未保存配置的连接测试、取消和模型发现。
3. 导入 PDF/DOCX/PPTX/XLSX。扫描 PDF 先使用 Apple Vision 检查文字，再翻译。
4. 点击译文编辑。关闭编辑器后草稿保存在本机；“保存全部草稿”一次提交。导出可选择保存草稿或导出已保存版本。
5. 页面支持双指捏合、双指横向滚动、智能缩放、⌘+/⌘−/⌘0、⌘←/⌘→，段落按 100 段分页。侧栏方向键切换文档。搜索覆盖全文和草稿；清除筛选返回正常阅读位置。
6. 关闭窗口可以继续后台翻译；⌘Q 保存已完成批次后退出，重启可续翻。归档与废纸篓均保留草稿；有草稿的废纸篓任务不会自动清除。

## 导入旧资料库

先退出来源应用，再从“导入旧资料库…”选择 `DocTranslatorNative` 或 `DocTranslatorMac` 文件夹。复制原件、有效译文、任务进度、草稿、书签、模型和设置；导入过程可重试，重复内容不会产生重复任务。原件不被修改。

- 旧 Word、PPT 任务保留来源分段方式及备注开关。已迁入译文在第一次编辑前字节不变。
- 无法验证映射会显示说明并阻止错位写回。需要重新解析时使用“另译一份”。
- 缺少历史模型快照的任务会在续翻前请求确认。历史未知费用显示未知。
- 同机能解密的凭据会重新加密；跨机无法解密或有冲突时保留模型并提示补录。
- 导入报告只保存在预览资料库的 `migration-reports/`，不上传 GitHub。

## 构建与验收

```sh
bash macos/build_app.sh
NATIVE_TEST_PYTHON="$(command -v python3)" macos/scripts/swift_tool.sh test --package-path macos
.venv/bin/python -m pytest tests -q
.venv/bin/python macos/scripts/verify_engine.py \
  --engine 'macos/dist/文档翻译预览版.app/Contents/Resources/DocTranslatorEngine/DocTranslatorEngine' \
  --output /tmp/doc-translator-isolated-acceptance
.venv/bin/python macos/scripts/install_preview.py --app 'macos/dist/文档翻译预览版.app'
```

构建工具将产物放在 `~/Library/Application Support/DocTranslatorBuilds/`，`macos/dist` 是访问链接。测试使用临时资料库与本地模拟服务。没有 GenOffice 的 CI 会显式跳过 Office 页面渲染，PDF 渲染和四格式翻译/修订仍须通过。

包内有第三方许可证。当前为本机 ad-hoc 签名的预览交付，未进行 Developer ID 公证。正式 Release、替换稳定版与合并 main 属于后续交付。
