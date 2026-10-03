"""Measure full local OCR of a synthetic 128-page PDF and bounded API reads."""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from packaged_api import PackagedAPI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app')
    parser.add_argument('ocr_tool')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.pdf_fixtures import long_scan_pdf
    with tempfile.TemporaryDirectory(prefix='unified-long-pdf-') as temporary:
        root = Path(temporary)
        source = long_scan_pdf(root / 'Long128.pdf', pages=128)
        run = subprocess.run(['/usr/bin/time', '-l', args.ocr_tool, '--ocr-json-paged', str(source)],
                             capture_output=True, text=True, timeout=300, check=True)
        lines = json.loads(run.stdout)
        assert set(line['page'] for line in lines) == set(range(1, 129))
        memory = re.search(r'(\d+)\s+maximum resident set size', run.stderr)
        with PackagedAPI(args.app, root) as api:
            task = api.upload(source)
            identifier = task['task_id']
            assert task['ocr_pages'] == 128
            for page in (1, 128):
                task = api.json(f'/api/tasks/{identifier}/ocr/page/{page}', {'lines': [line for line in lines if line['page'] == page]})
            assert task['ocr_completed_pages'] == [1, 128]
            first = api.json(f'/api/tasks/{identifier}/segments?limit=100')
            assert len(first['segments']) <= 100
            image = api.bytes(f'/api/tasks/{identifier}/preview/pdf/128?variant=original')
            assert image.startswith(b'\x89PNG')
        report = {'pages': 128, 'ocr_pages_seen': 128, 'recognized_lines': len(lines),
                  'maximum_rss_bytes': int(memory.group(1)) if memory else None,
                  'bounded_segments': len(first['segments']), 'checkpoint_pages': [1, 128],
                  'last_page_rendered': True, 'real_model_calls': 0}
        Path(args.report).write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == '__main__':
    main()
