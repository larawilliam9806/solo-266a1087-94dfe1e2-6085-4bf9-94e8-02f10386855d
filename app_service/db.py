"""SQLite 持久化层。

设计要点
--------
- 单连接 + 进程内 RLock：采集线程与 API 线程共享，所有写入串行化。
- 批次页原始 JSON 落 ``batch_pages``：失败/重启后续采直接复用已保存页，
  绝不重复请求源。
- 发布在单个事务内完成：整批采齐 → 替换 items / 重写 published_items →
  更新 published_snapshot → 核对校订（corrections）→ 推进校订视图 revision；
  任一步失败则回滚，旧发布数据与校订结果保持不变。
- 发布带"新旧快照保护"：较早启动的批次后完成时，若已有更晚启动批次
  发布的数据，则拒绝覆盖（批次标记为 superseded）。
- 校订与发布共用同一把锁：校订视图 / 校订版下载一次加锁读出，
  必然对应同一完整源快照及校订状态。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    snapshot_token  TEXT NOT NULL,
    source_version  TEXT,
    total_pages     INTEGER,
    status          TEXT NOT NULL,              -- running|failed|invalid|completed|published|superseded
    error_code      TEXT,
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS batch_pages (
    batch_id    INTEGER NOT NULL,
    page_no     INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,                  -- 源返回的整页 JSON
    PRIMARY KEY (batch_id, page_no),
    FOREIGN KEY (batch_id) REFERENCES batches(id)
);

CREATE TABLE IF NOT EXISTS items (
    item_id     TEXT PRIMARY KEY,               -- 藏品编号
    name        TEXT NOT NULL,
    era         TEXT,
    material    TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS published_items (
    item_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    era         TEXT,
    material    TEXT
);

CREATE TABLE IF NOT EXISTS published_snapshot (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    batch_id        INTEGER NOT NULL,
    snapshot_token  TEXT NOT NULL,
    source_version  TEXT,
    published_at    TEXT NOT NULL,
    item_count      INTEGER NOT NULL
);

-- 人工校订：每条记录保留"所依据的源值"，供后续采集时核对
CREATE TABLE IF NOT EXISTS corrections (
    item_id         TEXT NOT NULL,
    field           TEXT NOT NULL,              -- 可校订字段：era | material
    basis_value     TEXT,                       -- 校订所依据的源值（留痕）
    corrected_value TEXT NOT NULL,              -- 校订值
    status          TEXT NOT NULL,              -- active | conflict
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (item_id, field)
);

-- 单行元数据：revision 在每次发布 / 校订变动时递增，
-- 校订页据此做乐观并发控制（旧页面提交一律拒绝并提示重载）
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""

# 批次终态集合
TERMINAL_STATUSES = {"failed", "invalid", "published", "superseded"}

# 允许人工校订的藏品字段（年代、材质）
CORRECTABLE_FIELDS = ("era", "material")


class CorrectionError(Exception):
    """校订操作失败。code 由 API 层映射为 HTTP 状态码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.db_path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---------------- 批次 ----------------

    def create_batch(self, source_url: str, snapshot_token: str,
                     source_version: str | None, total_pages: int | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO batches (source_url, snapshot_token, source_version, "
                "total_pages, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                (source_url, snapshot_token, source_version, total_pages, utc_now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_batch(self, batch_id: int, status: str,
                   error_code: str | None = None,
                   error_message: str | None = None,
                   finished: bool = False) -> None:
        with self._lock:
            if finished:
                self._conn.execute(
                    "UPDATE batches SET status = ?, error_code = ?, error_message = ?, "
                    "finished_at = ? WHERE id = ?",
                    (status, error_code, error_message, utc_now(), batch_id),
                )
            else:
                self._conn.execute(
                    "UPDATE batches SET status = ?, error_code = ?, error_message = ? "
                    "WHERE id = ?",
                    (status, error_code, error_message, batch_id),
                )
            self._conn.commit()

    def reset_to_running(self, batch_id: int) -> None:
        """续采：把失败批次重新置为 running 并清掉旧错误。"""
        with self._lock:
            self._conn.execute(
                "UPDATE batches SET status = 'running', error_code = NULL, "
                "error_message = NULL, finished_at = NULL WHERE id = ?",
                (batch_id,),
            )
            self._conn.commit()

    def update_batch_meta(self, batch_id: int, total_pages: int,
                          source_version: str | None) -> None:
        """回填首页探测得到的总页数与源版本。"""
        with self._lock:
            self._conn.execute(
                "UPDATE batches SET total_pages = ?, source_version = ? WHERE id = ?",
                (total_pages, source_version, batch_id),
            )
            self._conn.commit()

    def recover_on_startup(self) -> list[int]:
        """应用重启：上次未结束的 running 批次没有存活采集线程，转为 failed。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM batches WHERE status = 'running'"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            for bid in ids:
                self._conn.execute(
                    "UPDATE batches SET status = 'failed', error_code = 'interrupted', "
                    "error_message = '应用重启，批次中断，可续采复用已保存页', "
                    "finished_at = ? WHERE id = ?",
                    (utc_now(), bid),
                )
            self._conn.commit()
            return ids

    # ---------------- 页 ----------------

    def save_page(self, batch_id: int, page_no: int, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO batch_pages (batch_id, page_no, fetched_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, page_no, utc_now(), json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()

    def get_saved_pages(self, batch_id: int) -> dict[int, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT page_no, payload FROM batch_pages WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
            return {int(r["page_no"]): json.loads(r["payload"]) for r in rows}

    # ---------------- 校订 ----------------

    def _revision_locked(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'revision'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _bump_revision_locked(self) -> int:
        """发布或校订变动都会推进 revision；须在持锁事务内调用。"""
        rev = self._revision_locked() + 1
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(rev),),
        )
        return rev

    def _check_revision_locked(self, base_revision: int) -> None:
        """乐观并发控制：旧页面所依据的源值/校订已变时拒绝提交。"""
        if int(base_revision) != self._revision_locked():
            raise CorrectionError(
                "stale_revision",
                "源值或校订已发生变化（采集发布或其他校订），请刷新页面后重试",
            )

    def _published_map_locked(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT item_id, name, era, material FROM published_items"
        ).fetchall()
        return {r["item_id"]: dict(r) for r in rows}

    def _corrections_locked(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM corrections ORDER BY item_id, field"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_correction(self, item_id: str, field: str, value: str,
                          base_revision: int) -> None:
        """新增或修改校订。校订依据记录为当前源值；已存在时按当前源值重新依据。"""
        value = (value or "").strip()
        with self._lock:
            self._check_revision_locked(base_revision)
            if field not in CORRECTABLE_FIELDS:
                raise CorrectionError(
                    "bad_field", f"字段 {field!r} 不支持校订（仅年代 era、材质 material）")
            item = self._published_map_locked().get(item_id)
            if item is None:
                raise CorrectionError(
                    "item_not_found", f"藏品 {item_id} 不在当前发布数据集中")
            if not value:
                raise CorrectionError("empty_value", "校订值不能为空")
            source_value = item[field]
            if value == source_value:
                raise CorrectionError(
                    "same_as_source", "校订值与当前源值一致，无需校订")
            now = utc_now()
            self._conn.execute(
                "INSERT INTO corrections (item_id, field, basis_value, corrected_value, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?) "
                "ON CONFLICT(item_id, field) DO UPDATE SET "
                "basis_value = excluded.basis_value, "
                "corrected_value = excluded.corrected_value, "
                "status = 'active', updated_at = excluded.updated_at",
                (item_id, field, source_value, value, now, now),
            )
            self._bump_revision_locked()
            self._conn.commit()

    def delete_correction(self, item_id: str, field: str, base_revision: int) -> None:
        """撤销校订：该字段恢复随源更新。"""
        with self._lock:
            self._check_revision_locked(base_revision)
            cur = self._conn.execute(
                "DELETE FROM corrections WHERE item_id = ? AND field = ?",
                (item_id, field),
            )
            if cur.rowcount == 0:
                raise CorrectionError("correction_not_found", "该字段没有校订记录")
            self._bump_revision_locked()
            self._conn.commit()

    def resolve_conflict(self, item_id: str, field: str, choice: str,
                         base_revision: int) -> None:
        """逐字段裁决冲突：choice=source 采用源值（校订作废）；
        choice=corrected 保留校订值（以当前源值重新作为校订依据）。"""
        with self._lock:
            self._check_revision_locked(base_revision)
            row = self._conn.execute(
                "SELECT * FROM corrections WHERE item_id = ? AND field = ?",
                (item_id, field),
            ).fetchone()
            if row is None:
                raise CorrectionError("correction_not_found", "该字段没有校订记录")
            if row["status"] != "conflict":
                raise CorrectionError("not_conflict", "该校订当前没有未决冲突")
            if choice == "source":
                self._conn.execute(
                    "DELETE FROM corrections WHERE item_id = ? AND field = ?",
                    (item_id, field),
                )
            elif choice == "corrected":
                item = self._published_map_locked().get(item_id)
                if item is None:  # 冲突校订的藏品必在发布集中，防御性检查
                    raise CorrectionError(
                        "item_not_found", f"藏品 {item_id} 不在当前发布数据集中")
                self._conn.execute(
                    "UPDATE corrections SET basis_value = ?, status = 'active', "
                    "updated_at = ? WHERE item_id = ? AND field = ?",
                    (item[field], utc_now(), item_id, field),
                )
            else:
                raise CorrectionError("bad_choice", "choice 必须是 source 或 corrected")
            self._bump_revision_locked()
            self._conn.commit()

    def get_corrected_view(self) -> dict[str, Any]:
        """校订视图：发布数据 + 校订状态在单把锁内一次读出，
        与发布事务互斥，保证视图对应同一完整源快照及校订状态。"""
        with self._lock:
            revision = self._revision_locked()
            snap = self._conn.execute(
                "SELECT * FROM published_snapshot WHERE id = 1"
            ).fetchone()
            published = dict(snap) if snap else None
            items = self._published_map_locked()
            corrections = self._corrections_locked()

        corr_map = {(c["item_id"], c["field"]): c for c in corrections}
        view_items: list[dict[str, Any]] = []
        for item_id in sorted(items):
            src = items[item_id]
            entry: dict[str, Any] = {"id": item_id, "name": src["name"]}
            for field in CORRECTABLE_FIELDS:
                source_value = src[field]
                corr = corr_map.get((item_id, field))
                field_view: dict[str, Any] = {
                    "source": source_value,
                    "effective": source_value,
                    "correction": None,
                }
                if corr is not None:
                    field_view["effective"] = corr["corrected_value"]
                    field_view["correction"] = {
                        "value": corr["corrected_value"],
                        "basis": corr["basis_value"],
                        "status": corr["status"],
                        "updated_at": corr["updated_at"],
                    }
                entry[field] = field_view
            view_items.append(entry)

        return {
            "revision": revision,
            "published": published,
            "correction_count": len(corrections),
            "conflict_count": sum(1 for c in corrections if c["status"] == "conflict"),
            "items": view_items,
        }

    def _reconcile_corrections_locked(self, new_items: dict[str, dict[str, Any]]) -> None:
        """发布新快照后在同一事务内核对全部校订：

        - 源已删除该藏品                  → 清除校订（校订版同步移除该藏品）；
        - 源值已等于校订值                → 差异自动消除，清除校订；
        - 源值仍等于校订依据（源未变）    → 沿用校订；
        - 源值偏离依据且不同于校订值      → 标记冲突，保留依据与校订值待裁决。
        """
        rows = self._conn.execute("SELECT * FROM corrections").fetchall()
        now = utc_now()
        for r in rows:
            item = new_items.get(r["item_id"])
            if item is None:
                self._conn.execute(
                    "DELETE FROM corrections WHERE item_id = ? AND field = ?",
                    (r["item_id"], r["field"]),
                )
                continue
            new_src = item.get(r["field"])
            if new_src == r["corrected_value"]:
                self._conn.execute(
                    "DELETE FROM corrections WHERE item_id = ? AND field = ?",
                    (r["item_id"], r["field"]),
                )
            elif new_src == r["basis_value"]:
                if r["status"] != "active":
                    self._conn.execute(
                        "UPDATE corrections SET status = 'active', updated_at = ? "
                        "WHERE item_id = ? AND field = ?",
                        (now, r["item_id"], r["field"]),
                    )
            else:
                if r["status"] != "conflict":
                    self._conn.execute(
                        "UPDATE corrections SET status = 'conflict', updated_at = ? "
                        "WHERE item_id = ? AND field = ?",
                        (now, r["item_id"], r["field"]),
                    )

    # ---------------- 发布 ----------------

    def get_published(self) -> dict[str, Any] | None:
        with self._lock:
            snap = self._conn.execute(
                "SELECT * FROM published_snapshot WHERE id = 1"
            ).fetchone()
            if not snap:
                return None
            items = self._conn.execute(
                "SELECT item_id, name, era, material FROM published_items ORDER BY item_id"
            ).fetchall()
            return {
                "snapshot": dict(snap),
                "items": [dict(r) for r in items],
            }

    def publish_batch(self, batch: dict[str, Any], pages: list[dict[str, Any]]) -> str:
        """整批发布。返回 'published' 或 'superseded'。

        新旧快照保护：若已发布数据来自更晚启动（id 更大）的批次，
        本批次不得覆盖，标记 superseded 并原样保留当前发布。
        """
        with self._lock:
            current = self._conn.execute(
                "SELECT batch_id FROM published_snapshot WHERE id = 1"
            ).fetchone()
            if current is not None and int(current["batch_id"]) > int(batch["id"]):
                self._conn.execute(
                    "UPDATE batches SET status = 'superseded', finished_at = ? WHERE id = ?",
                    (utc_now(), batch["id"]),
                )
                self._conn.commit()
                return "superseded"

            # 汇总整批藏品（以编号去重，后页覆盖前页）
            merged: dict[str, dict[str, Any]] = {}
            for page in pages:
                for item in page.get("items", []):
                    item_id = str(item["id"])
                    merged[item_id] = {
                        "item_id": item_id,
                        "name": str(item.get("name", "")),
                        "era": item.get("era"),
                        "material": item.get("material"),
                    }
            ordered = [merged[k] for k in sorted(merged)]
            now = utc_now()

            try:
                # 1) 全量重写 items（源中已不存在的藏品自然被移除）
                self._conn.execute("DELETE FROM items")
                self._conn.executemany(
                    "INSERT INTO items (item_id, name, era, material, updated_at) "
                    "VALUES (:item_id, :name, :era, :material, :updated_at)",
                    [{**it, "updated_at": now} for it in ordered],
                )
                # 2) 重写已发布视图
                self._conn.execute("DELETE FROM published_items")
                self._conn.executemany(
                    "INSERT INTO published_items (item_id, name, era, material) "
                    "VALUES (:item_id, :name, :era, :material)",
                    ordered,
                )
                # 3) 记录发布快照
                self._conn.execute(
                    "INSERT INTO published_snapshot (id, batch_id, snapshot_token, "
                    "source_version, published_at, item_count) "
                    "VALUES (1, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET batch_id = excluded.batch_id, "
                    "snapshot_token = excluded.snapshot_token, "
                    "source_version = excluded.source_version, "
                    "published_at = excluded.published_at, "
                    "item_count = excluded.item_count",
                    (batch["id"], batch["snapshot_token"], batch.get("source_version"),
                     now, len(ordered)),
                )
                # 4) 批次落终态
                self._conn.execute(
                    "UPDATE batches SET status = 'published', finished_at = ? WHERE id = ?",
                    (now, batch["id"]),
                )
                # 5) 核对校订（同事务）：沿用 / 自动消除 / 冲突 / 源删除清除；
                #    并推进 revision，使基于旧源值的校订页提交被拒绝
                self._reconcile_corrections_locked(merged)
                self._bump_revision_locked()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return "published"

    def close(self) -> None:
        with self._lock:
            self._conn.close()
