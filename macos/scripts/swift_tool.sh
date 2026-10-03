#!/usr/bin/env bash
# Keep the package build reproducible without changing xcode-select globally.
set -euo pipefail
DEV="${DOC_TRANSLATOR_XCODE:-/Applications/Xcode.app/Contents/Developer}"
SWIFT="$DEV/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift"
SDK="$DEV/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
if [[ ! -x "$SWIFT" ]]; then exec swift "$@"; fi
if DEVELOPER_DIR="$DEV" /usr/bin/xcrun --sdk macosx --show-sdk-path >/dev/null 2>&1; then
  export DEVELOPER_DIR="$DEV"
  exec "$SWIFT" "$@"
fi
# Read-only fallback to the installed command-line build tools. This does not
# accept a license or modify the global developer selection.
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
FRAMEWORKS="$DEV/Platforms/MacOSX.platform/Developer/Library/Frameworks"
"$SWIFT" "$@" --build-system native --sdk "$SDK" \
  -Xlinker -L -Xlinker "$DEV/Platforms/MacOSX.platform/Developer/usr/lib" \
  -Xswiftc -I -Xswiftc "$DEV/Platforms/MacOSX.platform/Developer/usr/lib" \
  -Xlinker -rpath -Xlinker "$DEV/Platforms/MacOSX.platform/Developer/usr/lib" \
  -Xswiftc -F -Xswiftc "$FRAMEWORKS" -Xlinker -rpath -Xlinker "$FRAMEWORKS"

if [[ "${1:-}" == test ]]; then
  PACKAGE=macos
  args=("$@")
  for ((i=0; i<${#args[@]}; i++)); do
    if [[ "${args[$i]}" == --package-path ]]; then PACKAGE="${args[$((i+1))]}"; fi
  done
  export NATIVE_TEST_PYTHON="${NATIVE_TEST_PYTHON:-$(command -v python3)}"
  # The CLT environment cannot discover XCTest, so run the compiled bundle
  # using Xcode's test host explicitly. A zero-test SPM result is not success.
  "$DEV/usr/bin/xctest" "$PACKAGE/.build/arm64-apple-macosx/debug/DocTranslatorMacPackageTests.xctest"
fi
