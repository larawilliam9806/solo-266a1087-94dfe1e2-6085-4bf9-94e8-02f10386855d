"""pytest 共享夹具。"""

from __future__ import annotations

import pytest

from harness import Harness


@pytest.fixture()
def harness(tmp_path):
    h = Harness(tmp_path)
    h.__enter__()
    # 源服务为进程内单例：每个用例开始前重置到初始状态（v1、清空令牌与故障注入）
    r = h.client.post("/admin/reset")
    assert r.status_code == 200
    yield h
    h.__exit__()
