# 0.3.0 统一预览版验收

日期：2026-10-03。目标：Apple Silicon、macOS 14+。构建与真实界面验收在本机 Apple Silicon Mac 上进行；产物为 ad-hoc 签名。

## 本机结果

| 检查 | 结果 |
|---|---|
| 后端 + 安装事务回归 | 275 passed，4 skipped（未提供的外部文档样本） |
| Ruff / Git whitespace | 通过 |
| 原生 XCTest | 17 个测试通过，包含 21 项原生分页、导入、生命周期和 Vision 子流程 |
| 四格式打包后验收 | DOCX/PPTX/XLSX/PDF：导入 → 本地模拟模型翻译 → 单段/批量修订 → 导出 → 重新解析 → 渲染，通过 |
| 模型快照 | 修改提供方地址/模型、批次设置后，续翻仍使用历史快照；新密钥生效 |
| 退出与进程 | 暂停/退出保存批次、重启续翻、父进程消失、随机令牌、同资料库双实例锁，通过 |
| 混合 PDF | 真实 Apple Vision、逐页检查点、低置信度确认、修订及双语导出通过；原页像素与链接保留 |
| 结构化 Word | 混合样式、链接、媒体及文本框兼容分支保留；实际 GenOffice 渲染通过 |
| 128 页扫描 PDF | 识别全部 128 页、3328 行；首末页检查点、末页渲染、100 段分页通过；逐页原生识别测试进程最大 RSS 1,585,889,280 字节 |
| 一万段文档 | 只传输当前 100 段；全文/草稿检索、排版筛选、跨页锚点定位通过 |
| 迁移 | 12 个合成迁移用例，覆盖 native v1/v2、MacBook v3、PPT 备注开关、草稿/书签、归档/废纸篓、模型 ID 冲突、凭据重加密、异常重试、源摘要与译文字节一致 |
| 磁盘故障 | 批量写回、元数据、草稿、密钥及安装失败保留旧版本；草稿保存中输入新文字保留 |
| 界面验收 | 双栏页面在 125% 显示正常；侧栏方向键切换；草稿跨文档、跨重启保留；页面模式与 125% 缩放重启恢复；未保存模型修改弹出选择 |
| 安装 | 独立预览 app 0.3.0 启动成功，首次设置独立密码；稳定版仍为 0.2.8 |

所有自动化使用隔离资料库、假密钥、本地模型服务。没有使用已保存的真实 API Key 进行模型请求。合成验收文件和真实迁移报告均保存在本机，未提交到仓库。

## 可复现命令

```sh
.venv/bin/python -m pytest tests macos/Verification/test_installer.py -q
.venv/bin/ruff check app tests
NATIVE_TEST_PYTHON="$PWD/.venv/bin/python" macos/scripts/swift_tool.sh test --package-path macos
bash macos/build_app.sh
.venv/bin/python macos/scripts/verify_engine.py \
  --engine 'macos/dist/文档翻译预览版.app/Contents/Resources/DocTranslatorEngine/DocTranslatorEngine' \
  --output /tmp/unified-engine-acceptance
.venv/bin/python macos/Verification/verify_packaged_word.py \
  'macos/dist/文档翻译预览版.app' --output /tmp/unified-word-acceptance
```

OCR 工具由 `macos/Verification/WorkflowTests.swift` 与原生状态、OCR、API 和持久化组件编译；`verify_packaged_pdf.py` 校验混合文档，`verify_long_pdf.py` 使用与 AppState 相同的逐页识别方式。内存数字包含 Apple Vision 模型与系统框架，不能视为所有文档的固定上限。

## 交付边界

- 已迁入本机现有任务、模型、设置和可用阅读位置；重复导入跳过相同记录。来源资料库已备份，原资料摘要验证一致。
- MacBook 资料库的兼容性使用合成夹具验证；本机没有那份真实资料库，可通过导入入口迁入。
- 旧版 Word 写回沿用旧编号；含链接、混合样式等复杂结构的 MacBook 旧任务会阻止有损重建，仍可导出现有译文。“另译一份”才使用结构化写回。无法验证映射的任务保留资料并禁止错位写回。
- Office 页面预览需要 GenOffice 或 LibreOffice。无转换器的 CI 显式记录该部分未渲染，本机已用实际 GenOffice 验证。
- 本机 Swift Package/XCTest 使用已安装工具链的显式路径兼容构建；未代替用户接受 Xcode 许可证，也未修改全局 xcode-select。
- 本轮交付独立预览应用、ZIP、SHA-256、构建来源与第三方许可证；未替换稳定版、未合并 main、未创建正式 Release。

## 云端 CI 与产物来源

[CI 37089331130](https://github.com/JiGuangYa/doc-translator/actions/runs/37089331130) 在应用源码提交 `ca630fd55d74706ae1735a915a45a0786ab972ae` 上全部通过：Linux Python 3.10/3.12、Windows Python 3.12、依赖审计、macos-15 原生测试、构建/签名、隔离冻结引擎验收和产物上传。

本机 ZIP 的 SHA-256：`6cfa727a527ee446a9d70a062dec630c67bb8d9a85598f35179849622dbb6abd`。签名与校验值以同目录的 `build-manifest.json` 和 `.sha256` 文件为准；后续仅验收文档提交不改变应用源码。
