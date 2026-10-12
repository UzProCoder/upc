"""Ma'lumotlar bazasi qatlami — PostgreSQL (Supabase) + asyncpg."""
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg

TZ = ZoneInfo("Asia/Tashkent")
IntegrityError = asyncpg.UniqueViolationError  # bot.py shuni ishlatadi

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id BIGINT PRIMARY KEY,
    full_name TEXT,
    username TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS projects(
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS responsibles(
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    tg_id BIGINT NOT NULL,
    name TEXT,
    PRIMARY KEY(project_id, tg_id)
);
CREATE TABLE IF NOT EXISTS tickets(
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id),
    user_id BIGINT NOT NULL,
    user_name TEXT,
    username TEXT,
    text TEXT,
    status TEXT DEFAULT 'new',
    handler_id BIGINT,
    handler_name TEXT,
    note TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_log(
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL,
    actor_id BIGINT,
    actor_name TEXT,
    kind TEXT,
    text TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications(
    ticket_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY(ticket_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_project ON tickets(project_id, status);
CREATE INDEX IF NOT EXISTS idx_log_ticket ON ticket_log(ticket_id);

-- Xavfsizlik: Supabase ochiq REST API (anon kalit) jadvallarga kira olmasin.
-- Bot "postgres" roli bilan ulanadi va RLS ni chetlab o'tadi.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE responsibles ENABLE ROW LEVEL SECURITY;
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticket_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
"""


def now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _pg(sql: str) -> str:
    """'?' belgilarini PostgreSQL uslubidagi $1, $2 ... ga almashtiradi."""
    n = 0

    def repl(_):
        nonlocal n
        n += 1
        return f"${n}"

    return re.sub(r"\?", repl, sql)


async def init_db() -> None:
    global _pool
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL topilmadi")
    _pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=5,
        ssl="require",
        statement_cache_size=0,  # Supabase pooler bilan mos
        command_timeout=30,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def close_db() -> None:
    if _pool:
        await _pool.close()


async def _fetchall(sql, params=()):
    async with _pool.acquire() as conn:
        rows = await conn.fetch(_pg(sql), *params)
        return [dict(r) for r in rows]


async def _fetchone(sql, params=()):
    rows = await _fetchall(sql, params)
    return rows[0] if rows else None


async def _execute(sql, params=()) -> int:
    """Ta'sirlangan qatorlar sonini qaytaradi."""
    async with _pool.acquire() as conn:
        status = await conn.execute(_pg(sql), *params)
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


async def _insert(sql, params=()) -> int:
    """INSERT qilib, yangi qator id sini qaytaradi."""
    async with _pool.acquire() as conn:
        return await conn.fetchval(_pg(sql + " RETURNING id"), *params)


# ------------------------------------------------------------ users
async def upsert_user(tg_id, full_name, username):
    await _execute(
        "INSERT INTO users(tg_id, full_name, username, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(tg_id) DO UPDATE SET full_name=EXCLUDED.full_name, username=EXCLUDED.username",
        (tg_id, full_name, username, now()),
    )


async def users_count() -> int:
    r = await _fetchone("SELECT COUNT(*) AS c FROM users")
    return r["c"]


# ------------------------------------------------------------ projects
async def add_project(name: str, description: str = "") -> int:
    return await _insert(
        "INSERT INTO projects(name, description, active, created_at) VALUES(?,?,1,?)",
        (name, description, now()),
    )


async def get_project(pid: int):
    return await _fetchone("SELECT * FROM projects WHERE id=?", (pid,))


async def list_projects(only_active: bool = False):
    sql = "SELECT * FROM projects"
    if only_active:
        sql += " WHERE active=1"
    return await _fetchall(sql + " ORDER BY lower(name)")


async def update_project(pid: int, **fields):
    allowed = {"name", "description", "active"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await _execute(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), pid))


async def delete_project(pid: int):
    await _execute("DELETE FROM projects WHERE id=?", (pid,))


async def project_ticket_count(pid: int) -> int:
    r = await _fetchone("SELECT COUNT(*) AS c FROM tickets WHERE project_id=?", (pid,))
    return r["c"]


# ------------------------------------------------------------ responsibles
async def add_responsible(pid: int, tg_id: int, name: str):
    await _execute(
        "INSERT INTO responsibles(project_id, tg_id, name) VALUES(?,?,?) "
        "ON CONFLICT(project_id, tg_id) DO UPDATE SET name=EXCLUDED.name",
        (pid, tg_id, name),
    )


async def remove_responsible(pid: int, tg_id: int):
    await _execute("DELETE FROM responsibles WHERE project_id=? AND tg_id=?", (pid, tg_id))


async def list_responsibles(pid: int):
    return await _fetchall("SELECT * FROM responsibles WHERE project_id=? ORDER BY name", (pid,))


async def responsible_project_ids(tg_id: int):
    rows = await _fetchall("SELECT project_id FROM responsibles WHERE tg_id=?", (tg_id,))
    return [r["project_id"] for r in rows]


async def is_responsible(pid: int, tg_id: int) -> bool:
    r = await _fetchone(
        "SELECT 1 AS x FROM responsibles WHERE project_id=? AND tg_id=?", (pid, tg_id)
    )
    return r is not None


async def staff_ids(pid: int, admin_ids) -> set:
    rows = await list_responsibles(pid)
    return set(admin_ids) | {r["tg_id"] for r in rows}


# ------------------------------------------------------------ tickets
async def create_ticket(project_id, user_id, user_name, username, text) -> int:
    ts = now()
    return await _insert(
        "INSERT INTO tickets(project_id, user_id, user_name, username, text, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,'new',?,?)",
        (project_id, user_id, user_name, username, text, ts, ts),
    )


async def get_ticket(tid: int):
    return await _fetchone(
        "SELECT t.*, p.name AS project_name FROM tickets t "
        "JOIN projects p ON p.id=t.project_id WHERE t.id=?",
        (tid,),
    )


async def set_status(tid, new_status, allowed_from, handler_id=None, handler_name=None, note=None) -> bool:
    """Holatni atomik o'zgartiradi (poyga holatlaridan himoya)."""
    ph = ",".join("?" * len(allowed_from))
    sets = "status=?, updated_at=?, note=?"
    params = [new_status, now(), note]
    if handler_id is not None:
        sets += ", handler_id=?, handler_name=?"
        params += [handler_id, handler_name]
    params += [tid, *allowed_from]
    n = await _execute(f"UPDATE tickets SET {sets} WHERE id=? AND status IN ({ph})", params)
    return n > 0


def _where(project_ids, status):
    conds, params = [], []
    if project_ids is not None:
        if not project_ids:
            return "1=0", []
        conds.append(f"t.project_id IN ({','.join('?' * len(project_ids))})")
        params += list(project_ids)
    if status != "all":
        conds.append("t.status=?")
        params.append(status)
    return (" AND ".join(conds) or "1=1"), params


async def list_tickets(project_ids, status, limit, offset):
    where, params = _where(project_ids, status)
    return await _fetchall(
        "SELECT t.*, p.name AS project_name FROM tickets t JOIN projects p ON p.id=t.project_id "
        f"WHERE {where} ORDER BY t.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )


async def count_tickets(project_ids, status) -> int:
    where, params = _where(project_ids, status)
    r = await _fetchone(f"SELECT COUNT(*) AS c FROM tickets t WHERE {where}", params)
    return r["c"]


async def status_counts(project_ids) -> dict:
    where, params = _where(project_ids, "all")
    rows = await _fetchall(
        f"SELECT t.status AS s, COUNT(*) AS c FROM tickets t WHERE {where} GROUP BY t.status", params
    )
    return {r["s"]: r["c"] for r in rows}


async def user_tickets(user_id, limit, offset):
    return await _fetchall(
        "SELECT t.*, p.name AS project_name FROM tickets t JOIN projects p ON p.id=t.project_id "
        "WHERE t.user_id=? ORDER BY t.id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )


async def count_user_tickets(user_id) -> int:
    r = await _fetchone("SELECT COUNT(*) AS c FROM tickets WHERE user_id=?", (user_id,))
    return r["c"]


async def active_user_tickets(user_id) -> int:
    r = await _fetchone(
        "SELECT COUNT(*) AS c FROM tickets WHERE user_id=? AND status IN ('new','accepted')",
        (user_id,),
    )
    return r["c"]


async def project_stats():
    return await _fetchall(
        "SELECT p.name AS name, COUNT(t.id) AS total, "
        "COALESCE(SUM(CASE WHEN t.status='new' THEN 1 ELSE 0 END),0) AS new_count "
        "FROM projects p LEFT JOIN tickets t ON t.project_id=p.id GROUP BY p.id ORDER BY p.name"
    )


# ------------------------------------------------------------ log / notifications
async def add_log(tid, actor_id, actor_name, kind, text=None):
    await _execute(
        "INSERT INTO ticket_log(ticket_id, actor_id, actor_name, kind, text, created_at) VALUES(?,?,?,?,?,?)",
        (tid, actor_id, actor_name, kind, text, now()),
    )


async def get_log(tid, limit=5):
    rows = await _fetchall(
        "SELECT * FROM ticket_log WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (tid, limit)
    )
    return list(reversed(rows))


async def save_notification(tid, chat_id, message_id):
    await _execute(
        "INSERT INTO notifications(ticket_id, chat_id, message_id) VALUES(?,?,?) "
        "ON CONFLICT(ticket_id, chat_id) DO UPDATE SET message_id=EXCLUDED.message_id",
        (tid, chat_id, message_id),
    )


async def get_notifications(tid):
    return await _fetchall("SELECT * FROM notifications WHERE ticket_id=?", (tid,))
