import asyncio
import time

import aiosqlite
import tiktoken

DEFAULT_TOKEN_LIMIT = 50_000
WINDOW_SECONDS = 60
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_time ON token_usage (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_time ON token_usage (created_at);
"""

ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(ENCODING.encode(text, disallowed_special=()))


class TokenRateLimiter:
    def __init__(
        self,
        db_path: str,
        token_limit: int = DEFAULT_TOKEN_LIMIT,
        window_seconds: float = WINDOW_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._token_limit = token_limit
        self._window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(
            self._db_path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        await self._db.executescript(SCHEMA)

    async def close(self) -> None:
        await self._db.close()

    async def reserve(self, tenant_id: str, tokens: int) -> int | None:
        async with self._lock:
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                now = time.time()
                window_start = now - self._window_seconds
                await self._db.execute(
                    "DELETE FROM token_usage WHERE created_at <= ?",
                    (window_start,),
                )
                rows = await self._db.execute_fetchall(
                    "SELECT COALESCE(SUM(tokens), 0) FROM token_usage "
                    "WHERE tenant_id = ? AND created_at > ?",
                    (tenant_id, window_start),
                )
                used_tokens = rows[0][0]

                reservation_id = None
                if used_tokens + tokens <= self._token_limit:
                    cursor = await self._db.execute(
                        "INSERT INTO token_usage (tenant_id, tokens, created_at) VALUES (?, ?, ?)",
                        (tenant_id, tokens, now),
                    )
                    reservation_id = cursor.lastrowid
                await self._db.commit()
                return reservation_id
            except BaseException:
                await self._db.rollback()
                raise

    async def settle(self, reservation_id: int, tokens: int) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE token_usage SET tokens = ? WHERE id = ?",
                (tokens, reservation_id),
            )
