"""Sample RSS and durable page progress for an isolated native OCR UI run."""

import argparse
import csv
import json
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=float, default=180)
    args = parser.parse_args()
    directory = Path(args.data_dir)
    if directory.name == "DocTranslatorNative":
        raise SystemExit("Profile an isolated test app")
    start = time.monotonic()
    samples = []
    with Path(args.output).open("w") as file:
        writer = csv.DictWriter(file, fieldnames=["seconds", "rss_mib", "completed", "required", "status"])
        writer.writeheader()
        while time.monotonic() - start < args.seconds:
            process = subprocess.run(["ps", "-p", str(args.pid), "-o", "rss="], capture_output=True, text=True, check=False)
            if process.returncode or not process.stdout.strip():
                break
            job = None
            for path in (directory / "tasks").glob("*/job.json"):
                candidate = json.loads(path.read_text())
                if candidate.get("filename") == args.filename:
                    job = candidate
            row = {"seconds": round(time.monotonic() - start, 2),
                   "rss_mib": round(int(process.stdout.strip()) / 1024, 2),
                   "completed": len((job or {}).get("ocr_completed_pages", [])),
                   "required": len((job or {}).get("ocr_required_pages", [])),
                   "status": (job or {}).get("status", "waiting")}
            writer.writerow(row)
            file.flush()
            samples.append(row)
            if row["required"] and row["completed"] == row["required"]:
                break
            time.sleep(0.25)
    print(json.dumps({"samples": len(samples), "peak_rss_mib": max((row["rss_mib"] for row in samples), default=0),
                      "last": samples[-1] if samples else None}))


if __name__ == "__main__":
    main()
