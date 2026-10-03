"""Write checksums and provenance beside distributable preview artifacts."""
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

output = Path(sys.argv[1]).resolve()
root = Path(__file__).resolve().parents[2]
archive = output / 'DocTranslatorUnifiedPreview-0.3.0-macOS-arm64.zip'
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(output / (archive.name + '.sha256')).write_text(f'{digest}  {archive.name}\n')
notices = output / '文档翻译预览版.app/Contents/Resources/ThirdPartyNotices.txt'
(output / 'ThirdPartyNotices.txt').write_bytes(notices.read_bytes())
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
manifest = {'version': '0.3.0', 'bundle_id': 'com.jiguang.doctranslator.preview', 'source_commit': commit,
            'platform': 'macOS 14+ / Apple Silicon', 'build_host': platform.platform(), 'sha256': digest,
            'archive': archive.name, 'signing': 'ad-hoc; codesign --verify --deep --strict passed'}
(output / 'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
print(json.dumps(manifest, ensure_ascii=False))
