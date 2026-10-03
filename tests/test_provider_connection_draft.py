"""The native settings screen can test credentials before saving them."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import config, store
from app.services.llm import client as llm_client
from app.services import ssrf_guard


def test_unsaved_provider_connection_uses_entered_details_without_persisting(
    client, tmp_path, monkeypatch
):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/v1/models":
                self.send_error(404)
                return
            data = json.dumps({"object": "list", "data": [
                {"id": "test-model", "object": "model", "created": 1,
                 "owned_by": "local"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            reply = {
                "id": "test-response", "object": "chat.completion", "created": 1,
                "model": "test-model", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop"}],
            }
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(ssrf_guard, "_ENABLED", False)
    providers_file = tmp_path / "providers.json"
    monkeypatch.setattr(config, "PROVIDERS_FILE", providers_file)
    entered_url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions/v1"
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        response = client.post("/api/providers/test", json={
            "base_url": entered_url,
            "model": "test-model", "api_key": "test-secret",
        })
        models = client.post("/api/providers/models", json={
            "base_url": entered_url, "api_key": "test-secret"})
        # Older saved providers with the same malformed URL must still work.
        monkeypatch.setattr(store, "get_provider", lambda _: {
            "base_url": entered_url, "model": "test-model"})
        monkeypatch.setattr(store, "get_provider_secret", lambda _: "test-secret")
        saved_result = llm_client.test_provider("legacy")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert models.json()["models"] == ["test-model"]
    assert response.json()["normalized_base_url"] == base_url
    assert isinstance(response.json()["latency_ms"], int)
    assert saved_result["ok"] is True
    assert [item[0] for item in received] == ["/v1/chat/completions"] * 2
    assert received[0][1] == "Bearer test-secret"
    assert received[0][2]["model"] == "test-model"
    assert not providers_file.exists()
    assert "test-secret" not in response.text


def test_saving_full_endpoint_stores_base_url(client, tmp_path, monkeypatch):
    providers_file = tmp_path / "providers.json"
    monkeypatch.setattr(config, "PROVIDERS_FILE", providers_file)
    monkeypatch.setattr(store, "_stored_secret_ids", lambda: set())
    response = client.post("/api/providers", json={
        "name": "test", "model": "test-model", "api_key": "",
        "base_url": "https://example.com/custom/v1/chat/completions/v1",
        "input_price_per_million": 0.1, "output_price_per_million": 0.3,
    })
    assert response.status_code == 200
    assert response.json()["provider"]["base_url"] == "https://example.com/custom/v1"
    assert response.json()["provider"]["input_price_per_million"] == 0.1
    assert json.loads(providers_file.read_text())["providers"][0]["base_url"] == (
        "https://example.com/custom/v1")
