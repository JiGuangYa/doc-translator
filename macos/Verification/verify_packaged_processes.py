"""Verify the frozen companion can supervise a converter without launching an API."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = Path(sys.argv[1]) / "Contents/Resources/DocTranslatorEngine/DocTranslatorEngine"
    with tempfile.TemporaryDirectory(prefix="doc-translator-packaged-role-") as temporary:
        result = subprocess.run([
            str(executable), "--supervise-child", str(os.getpid()), "--",
            "/bin/echo", "owned-converter-ok"],
            capture_output=True, text=True, timeout=5,
            env=dict(os.environ, DOC_TRANSLATOR_DATA_DIR=temporary))
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "owned-converter-ok", result.stdout
        assert not list(Path(temporary).iterdir()), "Supervisor role unexpectedly initialized application data"
    print("PASS: packaged converter supervisor returns output and does not start an API")


if __name__ == "__main__":
    main()
