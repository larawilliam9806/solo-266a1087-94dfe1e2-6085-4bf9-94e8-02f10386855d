"""校订功能端到端测试。

覆盖：
1. 新增校订、校订视图与校订版下载（原始发布下载不受影响）
2. 发布时核对：源未变沿用 / 源=校订值自动消除 / 源偏离成冲突 / 源删除清除；
   未校订字段随源更新
3. 未决冲突禁止下载；逐字段裁决（采用源值 / 保留校订值）
4. 乐观并发控制：旧 revision 的提交（校订/撤销/裁决）一律 409
5. 重启保留校订与未决冲突；采集失败不改变校订结果
6. 参数校验与无发布数据时的行为
"""

from __future__ import annotations

import httpx

from harness import wait_status


def _publish(c: httpx.Client, harness) -> dict:
    """跑一个完整批次直到 published，返回批次信息。"""
    r = c.post("/api/batches", json={"source_url": harness.catalog_url})
    assert r.status_code == 201, r.text
    batch = r.json()
    wait_status(c, batch["id"], {"published"})
    return batch


def _view(c: httpx.Client) -> dict:
    r = c.get("/api/corrected")
    assert r.status_code == 200
    return r.json()


def _correct(c: httpx.Client, item: str, field: str, value: str, rev: int) -> dict:
    r = c.post("/api/corrections", json={
        "item_id": item, "field": field, "value": value, "base_revision": rev})
    assert r.status_code == 201, r.text
    return r.json()


def _field(view: dict, item_id: str, field: str) -> dict:
    item = next(it for it in view["items"] if it["id"] == item_id)
    return item[field]


def test_correction_create_view_and_download(harness):
    """新增校订后：视图显示源值+校订值+依据；校订版下载用校订值；
    原始发布下载保持可用且不受校订影响。"""
    srv = harness.start_app()
    try:
        with httpx.Client(base_url=srv.base_url, timeout=10) as c:
            _publish(c, harness)
            view = _view(c)
            assert view["correction_count"] == 0
            assert view["conflict_count"] == 0
            rev0 = view["revision"]

            view = _correct(c, "A001", "era", "商代早期", rev0)
            assert view["revision"] > rev0
            assert view["correction_count"] == 1
            f = _field(view, "A001", "era")
            assert f["source"] == "商代晚期"            # 源值仍可见
            assert f["effective"] == "商代早期"          # 生效值为校订值
            assert f["correction"]["basis"] == "商代晚期"  # 保留所依据的源值
            assert f["correction"]["status"] == "active"
            # 未校订字段无校订记录
            assert _field(view, "A001", "material")["correction"] is None

            # 修改同一字段校订：依据不变（源未变），校订值更新
            view = _correct(c, "A001", "era", "商代中期", view["revision"])
            f = _field(view, "A001", "era")
            assert f["correction"]["value"] == "商代中期"
            assert f["correction"]["basis"] == "商代晚期"
            assert view["correction_count"] == 1

            # 校订版下载：年代为校订值，材质随源；附校订留痕
            dl = c.get("/api/corrected/download")
            assert dl.status_code == 200
            assert "attachment" in dl.headers["content-disposition"]
            data = dl.json()
            assert data["dataset"] == "museum-open-catalog-corrected"
            assert data["corrections_revision"] == view["revision"]
            a001 = next(it for it in data["items"] if it["id"] == "A001")
            assert a001["era"] == "商代中期"
            assert a001["material"] == "青铜"
            assert a001["corrected_fields"]["era"] == {
                "source_value": "商代晚期", "corrected_value": "商代中期"}
            a002 = next(it for it in data["items"] if it["id"] == "A002")
            assert a002["corrected_fields"] == {}

            # 原始发布下载保持可用且不受校订影响
            raw = c.get("/api/published/download")
            assert raw.status_code == 200
            a001_raw = next(it for it in raw.json()["items"] if it["id"] == "A001")
            assert a001_raw["era"] == "商代晚期"
            assert "corrected_fields" not in a001_raw

            # 撤销校订：恢复随源
            r = c.delete("/api/corrections/A001/era",
                         params={"base_revision": view["revision"]})
            assert r.status_code == 200
            view = r.json()
            assert view["correction_count"] == 0
            assert _field(view, "A001", "era")["effective"] == "商代晚期"
    finally:
        srv.stop()


def test_corrections_reconciled_on_new_publish(harness):
    """发布新快照时核对校订：沿用 / 自动消除 / 冲突 / 源删除清除；
    未校订字段随源更新。"""
    srv = harness.start_app()
    try:
        with httpx.Client(base_url=srv.base_url, timeout=10) as c:
            _publish(c, harness)  # v1
            view = _view(c)
            # A001 年代：v2 不变 → 应沿用
            view = _correct(c, "A001", "era", "商代早期", view["revision"])
            # A002 年代：校订为 v2 将采用的值 → 应自动消除
            view = _correct(c, "A002", "era", "良渚文化晚期", view["revision"])
            # A003 材质：校订为与 v2 不同的值 → 应成冲突
            view = _correct(c, "A003", "material", "唐三彩釉陶", view["revision"])
            # A004 年代：v2 删除该藏品 → 校订应被清除
            view = _correct(c, "A004", "era", "南朝齐", view["revision"])
            assert view["correction_count"] == 4

            # 源切换到 v2 并发布
            assert harness.client.post(
                "/admin/switch-version", params={"version": "v2"}).status_code == 200
            _publish(c, harness)

            view = _view(c)
            ids = {it["id"] for it in view["items"]}
            assert "A004" not in ids                # 源删除 → 校订版同步移除
            assert view["correction_count"] == 2    # A001 沿用 + A003 冲突
            assert view["conflict_count"] == 1

            # 源未变 → 沿用校订
            f = _field(view, "A001", "era")
            assert f["effective"] == "商代早期"
            assert f["correction"]["status"] == "active"
            assert f["correction"]["basis"] == "商代晚期"
            # 未校订字段随源更新（A001 名称在 v2 被源修订）
            a001 = next(it for it in view["items"] if it["id"] == "A001")
            assert a001["name"] == "青铜兽面纹鼎"
            # 源已更正为校订值 → 差异自动消除，校订记录清除
            f = _field(view, "A002", "era")
            assert f["correction"] is None
            assert f["effective"] == "良渚文化晚期"
            # 源值偏离校订依据且不同于校订值 → 冲突，依据与校订值保留
            f = _field(view, "A003", "material")
            assert f["source"] == "三彩釉陶"
            assert f["correction"]["status"] == "conflict"
            assert f["correction"]["basis"] == "陶胎三彩釉"
            assert f["correction"]["value"] == "唐三彩釉陶"
    finally:
        srv.stop()


def test_conflict_blocks_download_and_per_field_resolution(harness):
    """未决冲突禁止下载校订版；逐字段裁决（采用源值 / 保留校订值）后放行。"""
    srv = harness.start_app()
    try:
        with httpx.Client(base_url=srv.base_url, timeout=10) as c:
            _publish(c, harness)  # v1
            view = _view(c)
            # 制造两个将冲突的校订（v2 中源值会改成别的值）
            view = _correct(c, "A002", "era", "良渚文化早期", view["revision"])
            view = _correct(c, "A003", "material", "唐三彩釉陶", view["revision"])

            assert harness.client.post(
                "/admin/switch-version", params={"version": "v2"}).status_code == 200
            _publish(c, harness)

            view = _view(c)
            assert view["conflict_count"] == 2

            # 未决冲突 → 禁止下载校订版；原始发布下载不受影响
            r = c.get("/api/corrected/download")
            assert r.status_code == 409
            assert "冲突" in r.json()["detail"]
            assert c.get("/api/published/download").status_code == 200

            # 非法裁决选项 → 400
            r = c.post("/api/corrections/A002/era/resolve",
                       json={"choice": "bogus", "base_revision": view["revision"]})
            assert r.status_code == 400

            # 逐字段裁决：A002 保留校订值（依据重置为当前源值）
            r = c.post("/api/corrections/A002/era/resolve",
                       json={"choice": "corrected", "base_revision": view["revision"]})
            assert r.status_code == 200
            view = r.json()
            f = _field(view, "A002", "era")
            assert f["correction"]["status"] == "active"
            assert f["correction"]["basis"] == "良渚文化晚期"  # 以当前源值重新为依据
            assert f["effective"] == "良渚文化早期"            # 校订值保留
            assert view["conflict_count"] == 1

            # 仍有未决冲突 → 仍禁止下载
            assert c.get("/api/corrected/download").status_code == 409

            # A003 采用源值 → 校订作废
            r = c.post("/api/corrections/A003/material/resolve",
                       json={"choice": "source", "base_revision": view["revision"]})
            assert r.status_code == 200
            view = r.json()
            assert view["conflict_count"] == 0
            f = _field(view, "A003", "material")
            assert f["correction"] is None
            assert f["effective"] == "三彩釉陶"

            # 全部裁决完成 → 可下载，内容反映裁决结果
            dl = c.get("/api/corrected/download")
            assert dl.status_code == 200
            data = dl.json()
            a002 = next(it for it in data["items"] if it["id"] == "A002")
            assert a002["era"] == "良渚文化早期"
            a003 = next(it for it in data["items"] if it["id"] == "A003")
            assert a003["material"] == "三彩釉陶"
            assert a003["corrected_fields"] == {}
    finally:
        srv.stop()


def test_stale_page_submission_rejected(harness):
    """旧页面所依据的源值或校订已变（校订变动 / 新发布）→ 提交 409 并提示重载。"""
    srv = harness.start_app()
    try:
        with httpx.Client(base_url=srv.base_url, timeout=10) as c:
            _publish(c, harness)
            rev0 = _view(c)["revision"]

            # 另一会话修改了校订 → revision 前进
            _correct(c, "A001", "era", "商代早期", rev0)

            # 旧页面提交新校订 → 409
            r = c.post("/api/corrections", json={
                "item_id": "A002", "field": "era", "value": "新石器时代",
                "base_revision": rev0})
            assert r.status_code == 409
            assert "刷新" in r.json()["detail"]
            # 旧页面撤销校订 → 409
            r = c.delete("/api/corrections/A001/era",
                         params={"base_revision": rev0})
            assert r.status_code == 409
            # 旧页面裁决 → 409
            r = c.post("/api/corrections/A001/era/resolve",
                       json={"choice": "source", "base_revision": rev0})
            assert r.status_code == 409

            # 采集发布同样使旧页面失效
            rev1 = _view(c)["revision"]
            assert harness.client.post(
                "/admin/switch-version", params={"version": "v2"}).status_code == 200
            _publish(c, harness)
            r = c.post("/api/corrections", json={
                "item_id": "A002", "field": "era", "value": "新石器时代",
                "base_revision": rev1})
            assert r.status_code == 409

            # 刷新后（携带新 revision）提交成功
            rev2 = _view(c)["revision"]
            assert rev2 > rev1
            view = _correct(c, "A002", "era", "新石器时代", rev2)
            assert _field(view, "A002", "era")["effective"] == "新石器时代"
    finally:
        srv.stop()


def test_corrections_survive_restart_and_failed_collection(harness):
    """重启保留校订与未决冲突；采集失败不改变校订结果与已发布数据。"""
    srv1 = harness.start_app()
    try:
        with httpx.Client(base_url=srv1.base_url, timeout=10) as c:
            _publish(c, harness)  # v1
            view = _view(c)
            view = _correct(c, "A001", "era", "商代早期", view["revision"])
            _correct(c, "A003", "material", "唐三彩釉陶", view["revision"])
            assert harness.client.post(
                "/admin/switch-version", params={"version": "v2"}).status_code == 200
            _publish(c, harness)  # v2：A001 沿用，A003 冲突
            view = _view(c)
            assert view["correction_count"] == 2
            assert view["conflict_count"] == 1
    finally:
        srv1.stop()

    srv2 = harness.start_app()  # 同一数据库文件，模拟重启
    try:
        with httpx.Client(base_url=srv2.base_url, timeout=10) as c:
            view = _view(c)
            assert view["correction_count"] == 2      # 校订保留
            assert view["conflict_count"] == 1        # 未决冲突保留
            f = _field(view, "A003", "material")
            assert f["correction"]["status"] == "conflict"
            assert f["correction"]["value"] == "唐三彩釉陶"
            assert _field(view, "A001", "era")["effective"] == "商代早期"
            # 未决冲突在重启后仍禁止下载
            assert c.get("/api/corrected/download").status_code == 409

            # 采集失败不改变校订结果：v3 第 2 页注入一次性故障
            assert harness.client.post(
                "/admin/switch-version", params={"version": "v3"}).status_code == 200
            assert harness.client.post(
                "/admin/fail-page-once",
                params={"version": "v3", "page": 2}).status_code == 200
            r = c.post("/api/batches", json={"source_url": harness.catalog_url})
            assert r.status_code == 201
            wait_status(c, r.json()["id"], {"failed"})

            after = _view(c)
            assert after["correction_count"] == 2
            assert after["conflict_count"] == 1
            assert _field(after, "A001", "era")["effective"] == "商代早期"
            assert _field(after, "A003", "material")["correction"]["status"] == "conflict"
            # 已发布数据也未被失败批次触碰（仍是 v2）
            pub = c.get("/api/published").json()
            assert pub["snapshot"]["source_version"] == "v2"
    finally:
        srv2.stop()


def test_correction_validation_and_empty_state(harness):
    """参数校验：无发布数据、未知藏品、非法字段、空值、与源值相同、
    撤销/裁决不存在的校订、裁决非冲突校订。"""
    srv = harness.start_app()
    try:
        with httpx.Client(base_url=srv.base_url, timeout=10) as c:
            # 无发布数据：视图可用但为空；下载 404；提交校订 404
            view = _view(c)
            assert view["published"] is None and view["items"] == []
            assert c.get("/api/corrected/download").status_code == 404
            r = c.post("/api/corrections", json={
                "item_id": "A001", "field": "era", "value": "商代早期",
                "base_revision": view["revision"]})
            assert r.status_code == 404

            _publish(c, harness)
            rev = _view(c)["revision"]

            # 不存在的藏品
            r = c.post("/api/corrections", json={
                "item_id": "X999", "field": "era", "value": "商代早期",
                "base_revision": rev})
            assert r.status_code == 404
            # 不可校订字段（名称不支持校订）
            r = c.post("/api/corrections", json={
                "item_id": "A001", "field": "name", "value": "改名",
                "base_revision": rev})
            assert r.status_code == 400
            # 空校订值
            r = c.post("/api/corrections", json={
                "item_id": "A001", "field": "era", "value": "  ",
                "base_revision": rev})
            assert r.status_code == 400
            # 校订值与源值相同 → 无意义
            r = c.post("/api/corrections", json={
                "item_id": "A001", "field": "era", "value": "商代晚期",
                "base_revision": rev})
            assert r.status_code == 400
            # 撤销不存在的校订
            r = c.delete("/api/corrections/A001/era",
                         params={"base_revision": rev})
            assert r.status_code == 404
            # 裁决非冲突校订
            view = _correct(c, "A001", "era", "商代早期", rev)
            r = c.post("/api/corrections/A001/era/resolve",
                       json={"choice": "source",
                             "base_revision": view["revision"]})
            assert r.status_code == 400
            # 校订页可访问
            assert c.get("/corrected").status_code == 200
    finally:
        srv.stop()
