"""Collect installed dependency notices into the distributable app."""
import importlib.metadata
import sys
from pathlib import Path

parts = ["DocTranslator — bundled Python dependency notices\n"]
python_license = Path(sys.base_prefix) / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'LICENSE.txt'
if not python_license.is_file():
    raise RuntimeError(f'Python runtime license is missing: {python_license}')
parts.append(f'Python {sys.version.split()[0]} runtime\n{python_license.read_text()}')
for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
    if dist.metadata['Name'].lower() in {'pytest', 'pyinstaller-hooks-contrib'}:
        continue
    parts.append(f"\n{'=' * 70}\n{dist.metadata['Name']} {dist.version}\n")
    texts = []
    for file in dist.files or []:
        if 'license' in file.name.lower() or 'notice' in file.name.lower() or 'copying' in file.name.lower():
            try:
                text = Path(dist.locate_file(file)).read_text()
                if len(text) < 300_000:
                    texts.append(text)
            except (OSError, UnicodeError):
                pass
    parts.extend(texts or [dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See package project for license terms.'])
Path(sys.argv[1]).write_text('\n'.join(parts), encoding='utf-8')
