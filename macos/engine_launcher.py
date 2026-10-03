"""Private stdio bootstrap for the bundled native app (no credentials on disk)."""
import asyncio
import fcntl
import json
import os
import signal
import socket
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--supervise-child":
        from app.services.process_supervisor import main as supervise
        return supervise(sys.argv[2:])
    bootstrap = json.loads(sys.stdin.readline())
    token = bootstrap["token"]
    data_dir = Path(bootstrap["data_dir"]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    # Different copies of the .app can share Application Support. Recovery
    # and generation cleanup must never run beside another writer.
    lease = (data_dir / ".desktop-engine.lock").open("a+")
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"protocol_version": 1, "error": "另一份文档翻译正在使用这个资料库。请先退出另一份应用，再点击重试。"}), flush=True)
        lease.close()
        return
    if getattr(sys, "frozen", False):
        os.environ["DOC_TRANSLATOR_RESOURCE_DIR"] = str(Path(sys._MEIPASS))
    os.environ["DOC_TRANSLATOR_DATA_DIR"] = str(data_dir)
    os.environ["DOC_TRANSLATOR_TASK_TTL_DAYS"] = "0"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", int(bootstrap.get("port", 0))))
    listener.listen(128)
    port = listener.getsockname()[1]
    os.environ["DOC_TRANSLATOR_PORT"] = str(port)
    os.environ["DOC_TRANSLATOR_DESKTOP"] = "1"
    os.environ.setdefault("DOC_TRANSLATOR_KEYRING_SERVICE", "com.jiguang.doctranslator.preview.providers")

    import uvicorn
    from app import desktop
    desktop.configure(token)
    from app.main import app
    from app.services import renderer, task_manager, process_runner
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def desktop_lifespan(application):
        async with lifespan(application):
            print(json.dumps({"port": port, "protocol_version": 1}), flush=True)
            try:
                yield
            finally:
                task_manager.begin_shutdown()
                process_runner.begin_shutdown()
                renderer.shutdown()

    app.router.lifespan_context = desktop_lifespan

    def watch_parent():
        # EOF covers normal quit and an unexpected parent crash.
        sys.stdin.read()
        task_manager.begin_shutdown()
        process_runner.begin_shutdown()
        renderer.shutdown()
        os.kill(os.getpid(), signal.SIGTERM)
        # Persisted completed batches survive a blocked provider request.
        watchdog = threading.Timer(165, lambda: os._exit(0))
        watchdog.daemon = True
        watchdog.start()

    threading.Thread(target=watch_parent, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                         log_level="warning", access_log=False))
    try:
        asyncio.run(server.serve(sockets=[listener]))
    finally:
        renderer.shutdown()
        listener.close()
        lease.close()


if __name__ == "__main__":
    main()
