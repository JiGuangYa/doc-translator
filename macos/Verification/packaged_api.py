"""A disposable HTTP client for the packaged companion, using no user settings."""

import http.cookiejar
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path


class PackagedAPI:
    def __init__(self, app, directory):
        self.directory = Path(directory)
        self.token = uuid.uuid4().hex * 2
        self.executable = Path(app) / "Contents/Resources/DocTranslatorEngine/DocTranslatorEngine"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __enter__(self):
        self.log = (self.directory / "engine.log").open("w")
        self.process = subprocess.Popen([str(self.executable)], stdout=self.log, stderr=self.log, stdin=subprocess.PIPE,
            env=dict(os.environ, DOC_TRANSLATOR_DATA_DIR=str(self.directory),
                     DOC_TRANSLATOR_DESKTOP="1", DOC_TRANSLATOR_PORT=str(self.port),
                     DOC_TRANSLATOR_PARENT_PID=str(os.getpid()), PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring"))
        self.process.stdin.write((json.dumps({"token": self.token, "data_dir": str(self.directory), "port": self.port}) + "\n").encode())
        self.process.stdin.flush()
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    self.json("/healthz")
                    break
                except OSError:
                    if self.process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Packaged engine did not become healthy") from None
                    time.sleep(0.1)
            self.json("/api/auth/setup", {"password": "isolated-pdf-verification"})
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.log.close()

    def bytes(self, route):
        with self.opener.open(urllib.request.Request(f"http://127.0.0.1:{self.port}" + route, headers={"X-DocTranslator-Token": self.token}), timeout=30) as response:
            return response.read()

    def json(self, route, body=None, method=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}" + route,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"X-DocTranslator-Token": self.token, "Content-Type": "application/json"})
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    def upload(self, path):
        boundary = uuid.uuid4().hex
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(path).name}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode()
        body = header + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/upload", data=body,
            headers={"X-DocTranslator-Token": self.token, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)
