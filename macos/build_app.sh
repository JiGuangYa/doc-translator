#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  uv venv -p 3.12 .venv
fi
DIST_DIR="$(.venv/bin/python "$ROOT/macos/scripts/bundle_output.py" prepare "$ROOT")"
uv pip install --python .venv/bin/python -r requirements.txt -r macos/requirements-build.txt

# Use a full Xcode without changing the machine-wide developer selection.
if [[ -z "${DEVELOPER_DIR:-}" ]]; then
  for candidate in /Applications/Xcode.app/Contents/Developer /Applications/Xcode-beta.app/Contents/Developer; do
    if [[ -d "$candidate/Platforms/MacOSX.platform" ]]; then
      export DEVELOPER_DIR="$candidate"
      break
    fi
  done
fi
"$ROOT/macos/scripts/swift_tool.sh" build --package-path "$ROOT/macos" -c release
BIN_DIR="$("$ROOT/macos/scripts/swift_tool.sh" build --package-path "$ROOT/macos" -c release --show-bin-path)"

ICON_DEV="$DEVELOPER_DIR"
if ! /usr/bin/xcrun --sdk macosx --show-sdk-path >/dev/null 2>&1; then
  export DEVELOPER_DIR=/Library/Developer/CommandLineTools
fi
.venv/bin/python -m PyInstaller --noconfirm --clean --onedir --name DocTranslatorEngine \
  --paths "$ROOT" \
  --add-data "$ROOT/app/i18n:app/i18n" \
  --add-data "$ROOT/static:static" \
  --add-data "$ROOT/pyproject.toml:." \
  --collect-submodules app \
  --collect-submodules keyring \
  --collect-submodules openai \
  --collect-submodules fitz \
  --collect-all pymupdf \
  --collect-submodules uvicorn \
  --exclude-module pytest \
  --distpath "$DIST_DIR" \
  --workpath "$ROOT/macos/.pyinstaller" \
  --specpath "$ROOT/macos" \
  "$ROOT/macos/engine_launcher.py"

APP_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/doc-translator-build.XXXXXX")"
trap 'rm -rf "$APP_STAGE"' EXIT
APP="$APP_STAGE/文档翻译预览版.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN_DIR/DocTranslatorMac" "$APP/Contents/MacOS/DocTranslatorMac"
ditto "$DIST_DIR/DocTranslatorEngine" "$APP/Contents/Resources/DocTranslatorEngine"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleExecutable</key><string>DocTranslatorMac</string>
  <key>CFBundleIdentifier</key><string>com.jiguang.doctranslator.preview</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>文档翻译预览版</string>
  <key>CFBundleIconFile</key><string>DocTranslator</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.3.0</string>
  <key>CFBundleVersion</key><string>30</string>
  <key>CFBundleDocumentTypes</key><array><dict>
    <key>CFBundleTypeName</key><string>可翻译文档</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSHandlerRank</key><string>Alternate</string>
    <key>LSItemContentTypes</key><array>
      <string>com.adobe.pdf</string>
      <string>org.openxmlformats.wordprocessingml.document</string>
      <string>org.openxmlformats.presentationml.presentation</string>
      <string>org.openxmlformats.spreadsheetml.sheet</string>
    </array>
  </dict></array>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

"$ICON_DEV/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift" -sdk "$ICON_DEV/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk" "$ROOT/macos/scripts/make_icon.swift" "$APP_STAGE/DocTranslator.iconset"
iconutil -c icns "$APP_STAGE/DocTranslator.iconset" -o "$APP/Contents/Resources/DocTranslator.icns"
cp "$ROOT/LICENSE" "$APP/Contents/Resources/LICENSE-doc-translator.txt"
.venv/bin/python "$ROOT/macos/collect_licenses.py" "$APP/Contents/Resources/ThirdPartyNotices.txt"
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
.venv/bin/python "$ROOT/macos/scripts/bundle_output.py" publish "$APP" "$DIST_DIR"
.venv/bin/python "$ROOT/macos/scripts/release_manifest.py" "$DIST_DIR"
echo "Built $ROOT/macos/dist/文档翻译预览版.app"
