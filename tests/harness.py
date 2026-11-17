"""共享测试设施：真实 uvicorn 服务器（独立线程事件循环）+ 临时 SQLite 数据库。

供 test_e2e.py 与 test_corrections.py 复用；通过 conftest.py 的 harness
夹具注入，每个用例使用独立临时目录与独立端口。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from app_service.config import Settings
from app_service.main import create_app
from source_service.app import app as source_app


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    """uvicorn 运行在独立守护线程的事件循环中，跨测试用例稳定存活。"""

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        def _serve() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            config = uvicorn.Config(
                self.app, host="127.0.0.1", port=self.port,
                log_level="warning", lifespan="on",
            )
            self._server = uvicorn.Server(config)

            async def _watch_ready() -> None:
                while not self._server.started:
                    await asyncio.sleep(0.02)
                self._ready.set()

            loop.create_task(_watch_ready())
            try:
                loop.run_until_complete(self._server.serve())
            finally:
                loop.close()

        self._thread = threading.Thread(target=_serve, name=f"uvicorn-{self.prefix}", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError(f"{self.prefix} 服务器启动超时")

    def stop(self) -> None:
        assert self._server is not None and self._thread is not None
        self._server.should_exit = True
        self._thread.join(timeout=15)
        assert not self._thread.is_alive(), f"{self.prefix} 服务器停止超时"


class Harness:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.db_path = str(tmp_path / "museum.db")
        self.source = Server(source_app, "source")

    def __enter__(self) -> "Harness":
        self.source.start()
        self.client = httpx.Client(base_url=self.source.base_url, timeout=10)
        return self

    @property
    def catalog_url(self) -> str:
        return f"{self.source.base_url}/catalog"

    def build_app(self) -> Server:
        cfg = Settings(
            db_path=self.db_path,
            default_source_url=self.catalog_url,
            source_admin_url=f"{self.source.base_url}/",
            request_timeout=10,
            page_pause=0,
        )
        return Server(create_app(settings=cfg), "app")

    def start_app(self) -> Server:
        srv = self.build_app()
        srv.start()
        return srv

    def open_db(self):
        """以独立连接打开同一数据库文件（用于模拟崩溃现场）。"""
        from app_service.db import Database

        return Database(self.db_path)

    def __exit__(self, *exc) -> None:
        self.client.close()
        self.source.stop()


def wait_status(client: httpx.Client, batch_id: int,
                expected: set[str], timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        r = client.get(f"/api/batches/{batch_id}")
        assert r.status_code == 200
        last = r.json()
        if last["status"] in expected:
            return last
        time.sleep(0.1)
    raise AssertionError(f"批次 {batch_id} 未在超时内到达 {expected}，最后状态: {last}")
