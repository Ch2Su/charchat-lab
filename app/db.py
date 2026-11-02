"""SQLite 저장소.

메시지는 트리로 저장한다(parent_id). 채팅의 active_leaf_id에서 부모를 따라
루트까지 올라간 경로가 '지금 보고 있는 대화'다. 다시 생성(리롤)이나 수정은
같은 부모 아래에 형제 노드를 하나 더 만드는 것이라, 이전 답변이 사라지지 않는다.

스키마 버전은 PRAGMA user_version으로 관리한다. SCHEMA는 CREATE TABLE IF NOT EXISTS라서
이미 만들어진 DB에는 새 표나 열이 생기지 않는다. 그래서 SCHEMA(버전 1)는 그대로 두고,
바뀌는 부분은 MIGRATIONS에 버전을 붙여 덧붙인다. 열 때마다 DB 버전보다 높은 항목만
트랜잭션 하나씩으로 적용하므로, 중간에 실패하면 그 버전은 통째로 되돌려진다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  personality TEXT NOT NULL DEFAULT '',
  scenario TEXT NOT NULL DEFAULT '',
  first_mes TEXT NOT NULL DEFAULT '',
  mes_example TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL DEFAULT '',
  post_history_instructions TEXT NOT NULL DEFAULT '',
  creator_notes TEXT NOT NULL DEFAULT '',
  alternate_greetings TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  book_meta TEXT NOT NULL DEFAULT '{}',
  extensions TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lore_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '',
  keys TEXT NOT NULL DEFAULT '[]',
  secondary_keys TEXT NOT NULL DEFAULT '[]',
  content TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  constant INTEGER NOT NULL DEFAULT 0,
  selective INTEGER NOT NULL DEFAULT 0,
  case_sensitive INTEGER NOT NULL DEFAULT 0,
  insertion_order INTEGER NOT NULL DEFAULT 100,
  priority INTEGER NOT NULL DEFAULT 10
);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '',
  persona_name TEXT NOT NULL DEFAULT '',
  persona_desc TEXT NOT NULL DEFAULT '',
  active_leaf_id INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant','notice')),
  content TEXT NOT NULL,
  excluded INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(chat_id, parent_id);
CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  covers_until_msg_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mod_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER,
  message_id INTEGER,
  character_id INTEGER,
  stage TEXT NOT NULL,
  backend TEXT NOT NULL,
  category TEXT NOT NULL,
  action TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  message_id INTEGER,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SCHEMA_VERSION_BASE = 1  # 지금의 SCHEMA가 버전 1이다

# (버전, [SQL 문장, ...]). 버전은 2부터 1씩 올린다. 이미 배포된 항목은 고치지 않고 새 항목을 덧붙인다.
MIGRATIONS: list[tuple[int, list[str]]] = []

CHARACTER_TEXT_FIELDS = (
    "name", "description", "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions", "creator_notes",
)
CHARACTER_JSON_FIELDS = {"alternate_greetings": [], "tags": [], "book_meta": {}, "extensions": {}}
LORE_FIELDS = (
    "name", "keys", "secondary_keys", "content", "enabled", "constant", "selective",
    "case_sensitive", "insertion_order", "priority",
)


def _now() -> float:
    return time.time()


def _loads(value, fallback):
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        try:
            self._migrate()
        except BaseException:
            self.conn.close()  # 열다 실패한 연결이 파일을 잡고 있지 않게 한다
            raise

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        latest = max([SCHEMA_VERSION_BASE, *(version for version, _ in MIGRATIONS)])
        if current > latest:
            raise RuntimeError(f"이 DB는 더 새로운 버전의 코드로 만들어졌어요 (DB {current}, 코드 {latest}).")
        if current == 0:  # 새 DB이거나 마이그레이션 도입 전의 DB
            current = SCHEMA_VERSION_BASE
            self.conn.execute(f"PRAGMA user_version = {current}")
        for version, statements in sorted(MIGRATIONS, key=lambda item: item[0]):
            if version <= current:
                continue
            # executescript는 실행 전에 암묵적으로 COMMIT해서 롤백이 깨지므로 한 문장씩 실행한다
            with self.tx():
                for sql in statements:
                    self.conn.execute(sql)
                self.conn.execute(f"PRAGMA user_version = {int(version)}")
            current = version

    # ── 기본 쿼리 ──────────────────────────────────
    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def exec(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── 캐릭터 ────────────────────────────────────
    @staticmethod
    def _character_row(row: dict) -> dict:
        for field, fallback in CHARACTER_JSON_FIELDS.items():
            row[field] = _loads(row.get(field), fallback)
        return row

    @staticmethod
    def _lore_row(row: dict) -> dict:
        row["keys"] = _loads(row.get("keys"), [])
        row["secondary_keys"] = _loads(row.get("secondary_keys"), [])
        for flag in ("enabled", "constant", "selective", "case_sensitive"):
            row[flag] = bool(row.get(flag))
        return row

    def _write_lore(self, character_id: int, lore: list[dict]) -> None:
        self.exec("DELETE FROM lore_entries WHERE character_id=?", (character_id,))
        for entry in lore:
            self.exec(
                """INSERT INTO lore_entries(character_id, name, keys, secondary_keys, content, enabled,
                   constant, selective, case_sensitive, insertion_order, priority)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    character_id,
                    str(entry.get("name", "")),
                    _dumps([str(k) for k in entry.get("keys", []) if str(k).strip()]),
                    _dumps([str(k) for k in entry.get("secondary_keys", []) if str(k).strip()]),
                    str(entry.get("content", "")),
                    int(bool(entry.get("enabled", True))),
                    int(bool(entry.get("constant", False))),
                    int(bool(entry.get("selective", False))),
                    int(bool(entry.get("case_sensitive", False))),
                    int(entry.get("insertion_order", 100) or 0),
                    int(entry.get("priority", 10) or 0),
                ),
            )

    def create_character(self, data: dict, lore: list[dict] | None = None) -> int:
        now = _now()
        with self.tx():
            cid = self.exec(
                f"""INSERT INTO characters({", ".join(CHARACTER_TEXT_FIELDS)}, alternate_greetings, tags,
                    book_meta, extensions, created_at, updated_at)
                    VALUES({", ".join("?" for _ in CHARACTER_TEXT_FIELDS)}, ?, ?, ?, ?, ?, ?)""",
                (
                    *[str(data.get(f, "") or "") for f in CHARACTER_TEXT_FIELDS],
                    *[_dumps(data.get(f, fallback) or fallback) for f, fallback in CHARACTER_JSON_FIELDS.items()],
                    now,
                    now,
                ),
            )
            self._write_lore(cid, lore or [])
        return cid

    def update_character(self, character_id: int, data: dict, lore: list[dict] | None = None) -> None:
        sets, params = [], []
        for field in CHARACTER_TEXT_FIELDS:
            if field in data:
                sets.append(f"{field}=?")
                params.append(str(data[field] or ""))
        for field, fallback in CHARACTER_JSON_FIELDS.items():
            if field in data:
                sets.append(f"{field}=?")
                params.append(_dumps(data[field] or fallback))
        sets.append("updated_at=?")
        params.append(_now())
        with self.tx():
            self.exec(f"UPDATE characters SET {', '.join(sets)} WHERE id=?", (*params, character_id))
            if lore is not None:
                self._write_lore(character_id, lore)

    def get_character(self, character_id: int) -> dict | None:
        row = self.one("SELECT * FROM characters WHERE id=?", (character_id,))
        if not row:
            return None
        row = self._character_row(row)
        row["lore"] = [
            self._lore_row(r)
            for r in self.q(
                "SELECT * FROM lore_entries WHERE character_id=? ORDER BY insertion_order, id", (character_id,)
            )
        ]
        return row

    def list_characters(self) -> list[dict]:
        rows = self.q(
            """SELECT c.id, c.name, c.description, c.tags, c.updated_at,
                      (SELECT COUNT(*) FROM chats WHERE character_id=c.id) AS chat_count,
                      (SELECT COUNT(*) FROM lore_entries WHERE character_id=c.id) AS lore_count
               FROM characters c ORDER BY c.id"""
        )
        for row in rows:
            row["tags"] = _loads(row.get("tags"), [])
        return rows

    def delete_character(self, character_id: int) -> None:
        self.exec("DELETE FROM characters WHERE id=?", (character_id,))

    # ── 채팅 ──────────────────────────────────────
    def create_chat(self, character_id: int, persona_name: str, persona_desc: str = "", title: str = "") -> int:
        character = self.get_character(character_id)
        if not character:
            raise KeyError("character not found")
        now = _now()
        with self.tx():
            chat_id = self.exec(
                """INSERT INTO chats(character_id, title, persona_name, persona_desc, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (character_id, title, persona_name, persona_desc, now, now),
            )
            # 인사말과 대체 인사말은 모두 루트 노드(형제)로 둔다. 화면에서 넘겨 가며 고를 수 있다.
            greetings = [character["first_mes"], *character.get("alternate_greetings", [])]
            first_id = None
            for text in greetings:
                if not str(text).strip():
                    continue
                mid = self.add_message(chat_id, None, "assistant", str(text), meta={"greeting": True})
                first_id = first_id or mid
            if first_id:
                self.exec("UPDATE chats SET active_leaf_id=? WHERE id=?", (first_id, chat_id))
        return chat_id

    def get_chat(self, chat_id: int) -> dict | None:
        return self.one("SELECT * FROM chats WHERE id=?", (chat_id,))

    def list_chats(self, character_id: int | None = None) -> list[dict]:
        sql = """SELECT ch.*, c.name AS character_name,
                        (SELECT COUNT(*) FROM messages m WHERE m.chat_id=ch.id) AS message_count
                 FROM chats ch JOIN characters c ON c.id=ch.character_id"""
        if character_id is not None:
            return self.q(sql + " WHERE ch.character_id=? ORDER BY ch.updated_at DESC", (character_id,))
        return self.q(sql + " ORDER BY ch.updated_at DESC")

    def delete_chat(self, chat_id: int) -> None:
        self.exec("DELETE FROM chats WHERE id=?", (chat_id,))

    def set_active_leaf(self, chat_id: int, message_id: int | None) -> None:
        self.exec("UPDATE chats SET active_leaf_id=?, updated_at=? WHERE id=?", (message_id, _now(), chat_id))

    def update_chat(self, chat_id: int, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("title", "persona_name", "persona_desc")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        self.exec(f"UPDATE chats SET {sets}, updated_at=? WHERE id=?", (*allowed.values(), _now(), chat_id))

    # ── 메시지 트리 ────────────────────────────────
    @staticmethod
    def _message_row(row: dict) -> dict:
        row["meta"] = _loads(row.get("meta"), {})
        row["excluded"] = bool(row.get("excluded"))
        return row

    def add_message(
        self, chat_id: int, parent_id: int | None, role: str, content: str,
        meta: dict | None = None, excluded: bool = False,
    ) -> int:
        return self.exec(
            """INSERT INTO messages(chat_id, parent_id, role, content, excluded, meta, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (chat_id, parent_id, role, content, int(excluded), _dumps(meta or {}), _now()),
        )

    def update_message(self, message_id: int, *, content: str | None = None, meta: dict | None = None,
                       excluded: bool | None = None) -> None:
        sets, params = [], []
        if content is not None:
            sets.append("content=?")
            params.append(content)
        if meta is not None:
            sets.append("meta=?")
            params.append(_dumps(meta))
        if excluded is not None:
            sets.append("excluded=?")
            params.append(int(excluded))
        if sets:
            self.exec(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", (*params, message_id))

    def get_message(self, message_id: int) -> dict | None:
        row = self.one("SELECT * FROM messages WHERE id=?", (message_id,))
        return self._message_row(row) if row else None

    def path_to(self, message_id: int | None) -> list[dict]:
        """루트 → message_id 순서의 메시지 목록."""
        if message_id is None:
            return []
        rows = self.q(
            """WITH RECURSIVE path(id, depth) AS (
                   SELECT id, 0 FROM messages WHERE id=?
                   UNION ALL
                   SELECT m.parent_id, p.depth + 1 FROM messages m JOIN path p ON m.id = p.id
                   WHERE m.parent_id IS NOT NULL
               )
               SELECT messages.* FROM messages JOIN path ON messages.id = path.id
               ORDER BY path.depth DESC""",
            (message_id,),
        )
        return [self._message_row(r) for r in rows]

    def active_path(self, chat_id: int) -> list[dict]:
        chat = self.get_chat(chat_id)
        return self.path_to(chat["active_leaf_id"]) if chat else []

    def sibling_ids(self, chat_id: int, parent_id: int | None) -> list[int]:
        rows = self.q(
            "SELECT id FROM messages WHERE chat_id=? AND parent_id IS ? ORDER BY id", (chat_id, parent_id)
        )
        return [r["id"] for r in rows]

    def latest_leaf(self, message_id: int) -> int:
        """가장 최근에 만든 자식을 따라 내려가 잎 노드를 찾는다."""
        current = message_id
        while True:
            row = self.one("SELECT id FROM messages WHERE parent_id=? ORDER BY id DESC LIMIT 1", (current,))
            if not row:
                return current
            current = row["id"]

    # ── 요약 ──────────────────────────────────────
    def add_summary(self, chat_id: int, covers_until_msg_id: int, content: str, meta: dict | None = None) -> int:
        return self.exec(
            "INSERT INTO summaries(chat_id, covers_until_msg_id, content, meta, created_at) VALUES(?,?,?,?,?)",
            (chat_id, covers_until_msg_id, content, _dumps(meta or {}), _now()),
        )

    def summaries_for_chat(self, chat_id: int) -> list[dict]:
        rows = self.q("SELECT * FROM summaries WHERE chat_id=? ORDER BY id", (chat_id,))
        for row in rows:
            row["meta"] = _loads(row.get("meta"), {})
        return rows

    def latest_summary_on_path(self, chat_id: int, path: list[dict]) -> dict | None:
        """경로 위에 있는 메시지까지를 덮는 요약 중 가장 뒤쪽 것을 고른다.

        다른 가지에서 만든 요약은 그 가지의 메시지를 덮으므로 자연스럽게 제외된다.
        """
        position = {m["id"]: i for i, m in enumerate(path)}
        best, best_pos = None, -1
        for summary in self.summaries_for_chat(chat_id):
            pos = position.get(summary["covers_until_msg_id"])
            if pos is not None and pos >= best_pos:
                best, best_pos = summary, pos
        return best

    # ── 검수 로그 ──────────────────────────────────
    def add_mod_event(self, *, stage: str, backend: str, category: str, action: str, excerpt: str = "",
                      detail: dict | None = None, chat_id: int | None = None, message_id: int | None = None,
                      character_id: int | None = None) -> int:
        return self.exec(
            """INSERT INTO mod_events(chat_id, message_id, character_id, stage, backend, category, action,
               excerpt, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (chat_id, message_id, character_id, stage, backend, category, action, excerpt[:300],
             _dumps(detail or {}), _now()),
        )

    def list_mod_events(self, chat_id: int | None = None, limit: int = 100) -> list[dict]:
        if chat_id is None:
            rows = self.q("SELECT * FROM mod_events ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.q("SELECT * FROM mod_events WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
        for row in rows:
            row["detail"] = _loads(row.get("detail"), {})
        return rows

    def attach_mod_events(self, ids: list[int], message_id: int) -> None:
        for event_id in ids:
            self.exec("UPDATE mod_events SET message_id=? WHERE id=?", (message_id, event_id))

    # ── 프롬프트 로그 ──────────────────────────────
    def add_prompt_log(self, chat_id: int, message_id: int | None, kind: str, payload: dict) -> int:
        return self.exec(
            "INSERT INTO prompt_logs(chat_id, message_id, kind, payload, created_at) VALUES(?,?,?,?,?)",
            (chat_id, message_id, kind, _dumps(payload), _now()),
        )

    def prompt_log_for_message(self, message_id: int) -> dict | None:
        row = self.one(
            "SELECT * FROM prompt_logs WHERE message_id=? AND kind='chat' ORDER BY id DESC LIMIT 1", (message_id,)
        )
        if row:
            row["payload"] = _loads(row["payload"], {})
        return row

    def latest_prompt_log(self, chat_id: int, kind: str) -> dict | None:
        row = self.one(
            "SELECT * FROM prompt_logs WHERE chat_id=? AND kind=? ORDER BY id DESC LIMIT 1", (chat_id, kind)
        )
        if row:
            row["payload"] = _loads(row["payload"], {})
        return row

    # ── 설정 ──────────────────────────────────────
    def get_setting_overrides(self) -> dict:
        return {r["key"]: _loads(r["value"], None) for r in self.q("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value) -> None:
        self.exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _dumps(value)),
        )

    def clear_settings(self) -> None:
        self.exec("DELETE FROM settings")
