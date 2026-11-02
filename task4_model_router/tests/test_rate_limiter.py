import asyncio
import contextlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import pytest_asyncio

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import rate_limiter
from rate_limiter import (
    DEFAULT_TOKEN_LIMIT,
    ENCODING,
    WINDOW_SECONDS,
    TokenRateLimiter,
    count_tokens,
)

pytestmark = pytest.mark.asyncio

START_TIME = 1_800_000_000.0
SHORT_BUSY_TIMEOUT = 0.05
SUBPROCESS_TIMEOUT_SECONDS = 60

RESERVE_WORKER = """
import asyncio
import sys

from rate_limiter import TokenRateLimiter


async def main(db_path, attempts, tokens, limit):
    limiter = TokenRateLimiter(db_path, limit)
    await limiter.open()
    results = await asyncio.gather(*[limiter.reserve("shared", tokens) for _ in range(attempts)])
    await limiter.close()
    print(sum(result is not None for result in results))


asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])))
"""


class FakeClock:
    def __init__(self):
        self.now = START_TIME

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Interceptor:
    def __init__(self, connection, statement, action, also_cancel_rollback=False):
        self._connection = connection
        self._statement = statement
        self._action = action
        self._also_cancel_rollback = also_cancel_rollback
        self.fired = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def _intercept(self, statement):
        if self.fired or not statement.startswith(self._statement):
            return
        self.fired = True
        if self._action == "fail":
            raise sqlite3.OperationalError("disk I/O error")
        asyncio.current_task().cancel()

    async def execute(self, sql, parameters=None):
        self._intercept(sql)
        return await self._connection.execute(sql, parameters)

    async def execute_fetchall(self, sql, parameters=None):
        self._intercept(sql)
        return await self._connection.execute_fetchall(sql, parameters)

    async def commit(self):
        self._intercept("COMMIT")
        return await self._connection.commit()

    async def rollback(self):
        if self._also_cancel_rollback:
            asyncio.current_task().cancel()
        return await self._connection.rollback()


def read_rows(db_path):
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            "SELECT tenant_id, tokens FROM token_usage ORDER BY id"
        ).fetchall()


def read_created_at(db_path):
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        return [row[0] for row in connection.execute("SELECT created_at FROM token_usage ORDER BY id")]


def run_sql(db_path, statement):
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        connection.execute(statement)
        connection.commit()


def other_connection_can_write(db_path):
    with contextlib.closing(sqlite3.connect(db_path, timeout=0.1)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS write_probe (value INTEGER)")
        connection.execute("INSERT INTO write_probe VALUES (1)")
        connection.commit()
    return True


def block_insert(db_path):
    run_sql(
        db_path,
        "CREATE TRIGGER block_insert BEFORE INSERT ON token_usage "
        "BEGIN SELECT RAISE(ABORT, 'insert blocked'); END",
    )


def allow_insert(db_path):
    run_sql(db_path, "DROP TRIGGER block_insert")


@pytest.fixture
def clock(monkeypatch):
    fake_clock = FakeClock()
    monkeypatch.setattr(rate_limiter, "time", fake_clock)
    return fake_clock


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "usage.db")


@pytest_asyncio.fixture
async def limiter(db_path, clock):
    instance = TokenRateLimiter(db_path)
    await instance.open()
    yield instance
    await instance.close()


@pytest_asyncio.fixture
async def small_limiter(db_path, clock):
    instance = TokenRateLimiter(db_path, token_limit=1_000)
    await instance.open()
    yield instance
    await instance.close()


@pytest_asyncio.fixture
async def impatient_limiter(db_path, clock, monkeypatch):
    monkeypatch.setattr(rate_limiter, "SQLITE_BUSY_TIMEOUT_SECONDS", SHORT_BUSY_TIMEOUT)
    instance = TokenRateLimiter(db_path, token_limit=1_000)
    await instance.open()
    yield instance
    await instance.close()


async def test_count_tokens_matches_known_value():
    assert count_tokens("hello world") == 2


async def test_count_tokens_of_empty_text_is_zero():
    assert count_tokens("") == 0


async def test_count_tokens_matches_encoding():
    text = "The quick brown fox jumps over the lazy dog."
    assert count_tokens(text) == len(ENCODING.encode(text))


async def test_count_tokens_is_not_whitespace_based():
    assert count_tokens("unbroken" * 50) > 1


async def test_count_tokens_accepts_special_token_text():
    assert count_tokens("<|endoftext|>") > 0


async def test_default_limit_is_50000_tokens_per_60_seconds():
    assert DEFAULT_TOKEN_LIMIT == 50_000
    assert WINDOW_SECONDS == 60


async def test_reservation_at_exact_limit_is_allowed(limiter, db_path):
    assert await limiter.reserve("tenant-a", 50_000) is not None
    assert read_rows(db_path) == [("tenant-a", 50_000)]


async def test_reservation_one_token_over_limit_is_rejected(limiter, db_path):
    assert await limiter.reserve("tenant-a", 50_001) is None
    assert read_rows(db_path) == []


async def test_usage_accumulates_until_limit(limiter):
    assert await limiter.reserve("tenant-a", 30_000) is not None
    assert await limiter.reserve("tenant-a", 20_000) is not None
    assert await limiter.reserve("tenant-a", 1) is None


async def test_rejected_reservation_does_not_consume_quota(limiter):
    assert await limiter.reserve("tenant-a", 40_000) is not None
    assert await limiter.reserve("tenant-a", 20_000) is None
    assert await limiter.reserve("tenant-a", 10_000) is not None


async def test_reservations_store_clock_timestamp(limiter, db_path, clock):
    await limiter.reserve("tenant-a", 1)
    clock.advance(5)
    await limiter.reserve("tenant-a", 1)
    assert read_created_at(db_path) == [START_TIME, START_TIME + 5]


async def test_reservation_ids_are_unique(limiter):
    ids = [await limiter.reserve("tenant-a", 1) for _ in range(5)]
    assert len(set(ids)) == 5


@pytest.mark.parametrize(
    ("elapsed_seconds", "allowed"),
    [(0, False), (59.0, False), (59.999, False), (60.0, True), (60.001, True), (120.0, True)],
)
async def test_expiry_boundary(limiter, clock, elapsed_seconds, allowed):
    await limiter.reserve("tenant-a", 50_000)
    clock.advance(elapsed_seconds)
    assert (await limiter.reserve("tenant-a", 1) is not None) is allowed


async def test_window_slides_instead_of_resetting_each_calendar_minute(limiter, clock):
    clock.advance(30)
    await limiter.reserve("tenant-a", 50_000)
    clock.advance(31)
    assert await limiter.reserve("tenant-a", 1) is None


async def test_each_entry_expires_on_its_own_schedule(limiter, clock):
    await limiter.reserve("tenant-a", 30_000)
    clock.advance(30)
    await limiter.reserve("tenant-a", 20_000)
    clock.advance(29)
    assert await limiter.reserve("tenant-a", 1) is None
    clock.advance(1)
    assert await limiter.reserve("tenant-a", 30_000) is not None
    assert await limiter.reserve("tenant-a", 1) is None
    clock.advance(30)
    assert await limiter.reserve("tenant-a", 20_000) is not None


async def test_tenants_have_independent_quotas(limiter):
    assert await limiter.reserve("tenant-a", 50_000) is not None
    assert await limiter.reserve("tenant-a", 1) is None
    assert await limiter.reserve("tenant-b", 50_000) is not None


async def test_expired_rows_are_deleted_on_next_reservation(limiter, db_path, clock):
    await limiter.reserve("tenant-a", 100)
    clock.advance(60)
    await limiter.reserve("tenant-a", 200)
    assert read_rows(db_path) == [("tenant-a", 200)]


async def test_eviction_removes_rows_of_other_tenants(limiter, db_path, clock):
    await limiter.reserve("tenant-a", 100)
    clock.advance(60)
    await limiter.reserve("tenant-b", 200)
    assert read_rows(db_path) == [("tenant-b", 200)]


async def test_eviction_keeps_rows_inside_window(limiter, db_path, clock):
    await limiter.reserve("tenant-a", 100)
    clock.advance(59)
    await limiter.reserve("tenant-a", 200)
    assert read_rows(db_path) == [("tenant-a", 100), ("tenant-a", 200)]


async def test_rejected_reservation_still_commits_eviction(limiter, db_path, clock):
    await limiter.reserve("tenant-a", 100)
    clock.advance(60)
    assert await limiter.reserve("tenant-b", 50_001) is None
    assert read_rows(db_path) == []


async def test_concurrent_reservations_cannot_exceed_limit(small_limiter, db_path):
    results = await asyncio.gather(*[small_limiter.reserve("tenant-a", 100) for _ in range(200)])
    assert sum(result is not None for result in results) == 10
    assert sum(tokens for _, tokens in read_rows(db_path)) == 1_000


async def test_concurrent_reservations_of_mixed_sizes_stay_within_limit(small_limiter, db_path):
    sizes = [50, 150, 250, 350] * 25
    results = await asyncio.gather(*[small_limiter.reserve("tenant-a", size) for size in sizes])
    granted = sum(size for size, result in zip(sizes, results) if result is not None)
    assert granted <= 1_000
    assert sum(tokens for _, tokens in read_rows(db_path)) == granted


async def test_concurrent_tenants_are_limited_separately(small_limiter, db_path):
    tenants = ["tenant-a", "tenant-b", "tenant-c"]
    results = await asyncio.gather(
        *[small_limiter.reserve(tenant, 100) for tenant in tenants for _ in range(30)]
    )
    assert sum(result is not None for result in results) == 30
    rows = read_rows(db_path)
    for tenant in tenants:
        assert sum(tokens for row_tenant, tokens in rows if row_tenant == tenant) == 1_000


async def test_multiple_processes_cannot_exceed_limit(db_path):
    setup = TokenRateLimiter(db_path)
    await setup.open()
    await setup.close()

    workers = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            RESERVE_WORKER,
            db_path,
            "150",
            "10",
            "1000",
            cwd=APP_DIR,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(3)
    ]
    outputs = await asyncio.wait_for(
        asyncio.gather(*[worker.communicate() for worker in workers]),
        SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert [worker.returncode for worker in workers] == [0, 0, 0], outputs
    assert sum(int(stdout) for stdout, _ in outputs) == 100
    assert sum(tokens for _, tokens in read_rows(db_path)) == 1_000


async def test_usage_persists_after_reopen(db_path, clock):
    first = TokenRateLimiter(db_path)
    await first.open()
    await first.reserve("tenant-a", 50_000)
    await first.close()

    second = TokenRateLimiter(db_path)
    await second.open()
    try:
        assert await second.reserve("tenant-a", 1) is None
    finally:
        await second.close()


async def test_persisted_usage_still_expires_after_reopen(db_path, clock):
    first = TokenRateLimiter(db_path)
    await first.open()
    await first.reserve("tenant-a", 50_000)
    await first.close()

    clock.advance(60)
    second = TokenRateLimiter(db_path)
    await second.open()
    try:
        assert await second.reserve("tenant-a", 50_000) is not None
    finally:
        await second.close()
    assert read_rows(db_path) == [("tenant-a", 50_000)]


async def test_database_is_stored_on_disk(limiter, db_path):
    await limiter.reserve("tenant-a", 1)
    assert Path(db_path).stat().st_size > 0


async def test_settlement_below_reservation_frees_quota(small_limiter, db_path):
    reservation_id = await small_limiter.reserve("tenant-a", 1_000)
    await small_limiter.settle(reservation_id, 200)
    assert read_rows(db_path) == [("tenant-a", 200)]
    assert await small_limiter.reserve("tenant-a", 800) is not None
    assert await small_limiter.reserve("tenant-a", 1) is None


async def test_settlement_above_reservation_consumes_extra_quota(small_limiter, db_path):
    reservation_id = await small_limiter.reserve("tenant-a", 500)
    await small_limiter.settle(reservation_id, 1_200)
    assert read_rows(db_path) == [("tenant-a", 1_200)]
    assert await small_limiter.reserve("tenant-a", 1) is None


async def test_settlement_to_zero_refunds_reservation(small_limiter):
    reservation_id = await small_limiter.reserve("tenant-a", 1_000)
    await small_limiter.settle(reservation_id, 0)
    assert await small_limiter.reserve("tenant-a", 1_000) is not None


async def test_settlement_changes_only_the_target_row(small_limiter, db_path):
    first = await small_limiter.reserve("tenant-a", 100)
    await small_limiter.reserve("tenant-a", 200)
    await small_limiter.settle(first, 50)
    assert read_rows(db_path) == [("tenant-a", 50), ("tenant-a", 200)]


async def test_settlement_keeps_original_timestamp(small_limiter, db_path, clock):
    reservation_id = await small_limiter.reserve("tenant-a", 1_000)
    clock.advance(30)
    await small_limiter.settle(reservation_id, 1_000)
    assert read_created_at(db_path) == [START_TIME]
    clock.advance(30)
    assert await small_limiter.reserve("tenant-a", 1_000) is not None


async def test_settling_evicted_reservation_does_not_recreate_it(small_limiter, db_path, clock):
    reservation_id = await small_limiter.reserve("tenant-a", 100)
    clock.advance(60)
    await small_limiter.reserve("tenant-b", 200)
    await small_limiter.settle(reservation_id, 999)
    assert read_rows(db_path) == [("tenant-b", 200)]


async def test_reservation_ids_are_not_reused_after_eviction(small_limiter, db_path, clock):
    old_id = await small_limiter.reserve("tenant-a", 100)
    clock.advance(60)
    new_id = await small_limiter.reserve("tenant-b", 200)
    assert new_id > old_id
    await small_limiter.settle(old_id, 999)
    assert read_rows(db_path) == [("tenant-b", 200)]


@pytest.mark.parametrize("statement", ["BEGIN", "DELETE", "SELECT", "INSERT"])
async def test_cancellation_before_commit_rolls_back(small_limiter, db_path, statement):
    connection = small_limiter._db
    small_limiter._db = Interceptor(connection, statement, "cancel")
    task = asyncio.create_task(small_limiter.reserve("tenant-a", 100))

    with pytest.raises(asyncio.CancelledError):
        await task

    assert small_limiter._db.fired
    assert not connection.in_transaction
    assert read_rows(db_path) == []
    assert not small_limiter._lock.locked()


@pytest.mark.parametrize("statement", ["BEGIN", "DELETE", "SELECT", "INSERT"])
async def test_connection_is_usable_after_cancellation(small_limiter, db_path, statement):
    small_limiter._db = Interceptor(small_limiter._db, statement, "cancel")
    task = asyncio.create_task(small_limiter.reserve("tenant-a", 100))

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await small_limiter.reserve("tenant-a", 100) is not None
    assert read_rows(db_path) == [("tenant-a", 100)]


@pytest.mark.parametrize("statement", ["BEGIN", "DELETE", "SELECT", "INSERT", "COMMIT"])
async def test_cancellation_leaves_no_lock_for_other_connections(small_limiter, db_path, statement):
    small_limiter._db = Interceptor(small_limiter._db, statement, "cancel")
    task = asyncio.create_task(small_limiter.reserve("tenant-a", 100))

    with pytest.raises(asyncio.CancelledError):
        await task

    assert other_connection_can_write(db_path)


async def test_cancellation_during_commit_never_loses_a_committed_reservation(small_limiter, db_path):
    connection = small_limiter._db
    small_limiter._db = Interceptor(connection, "COMMIT", "cancel")
    task = asyncio.create_task(small_limiter.reserve("tenant-a", 100))

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not connection.in_transaction
    assert read_rows(db_path) == [("tenant-a", 100)]
    assert await small_limiter.reserve("tenant-a", 100) is not None


async def test_second_cancellation_during_rollback_leaves_connection_usable(small_limiter, db_path):
    connection = small_limiter._db
    small_limiter._db = Interceptor(connection, "SELECT", "cancel", also_cancel_rollback=True)
    task = asyncio.create_task(small_limiter.reserve("tenant-a", 100))

    with pytest.raises(asyncio.CancelledError):
        await task

    small_limiter._db = connection
    assert await small_limiter.reserve("tenant-a", 100) is not None
    assert not connection.in_transaction
    assert read_rows(db_path) == [("tenant-a", 100)]
    assert other_connection_can_write(db_path)


async def test_cancellation_while_waiting_for_lock_writes_nothing(small_limiter, db_path):
    await small_limiter._lock.acquire()
    task = asyncio.create_task(small_limiter.reserve("tenant-a", 100))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    small_limiter._lock.release()
    assert read_rows(db_path) == []
    assert await small_limiter.reserve("tenant-a", 100) is not None


async def test_cancelled_settlement_keeps_reservation(small_limiter, db_path):
    reservation_id = await small_limiter.reserve("tenant-a", 100)
    await small_limiter._lock.acquire()
    task = asyncio.create_task(small_limiter.settle(reservation_id, 0))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    small_limiter._lock.release()
    assert read_rows(db_path) == [("tenant-a", 100)]


async def test_cancelled_reservation_releases_lock_for_waiting_reservation(small_limiter, db_path):
    small_limiter._db = Interceptor(small_limiter._db, "SELECT", "cancel")
    cancelled = asyncio.create_task(small_limiter.reserve("tenant-a", 100))
    waiting = asyncio.create_task(small_limiter.reserve("tenant-a", 200))

    results = await asyncio.gather(cancelled, waiting, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], int)
    assert read_rows(db_path) == [("tenant-a", 200)]


async def test_failed_insert_rolls_back_and_recovers(small_limiter, db_path):
    block_insert(db_path)

    with pytest.raises(sqlite3.Error):
        await small_limiter.reserve("tenant-a", 100)

    assert not small_limiter._db.in_transaction
    assert not small_limiter._lock.locked()
    allow_insert(db_path)
    assert await small_limiter.reserve("tenant-a", 100) is not None
    assert read_rows(db_path) == [("tenant-a", 100)]


async def test_failed_insert_also_rolls_back_eviction(small_limiter, db_path, clock):
    await small_limiter.reserve("tenant-a", 100)
    clock.advance(60)
    block_insert(db_path)

    with pytest.raises(sqlite3.Error):
        await small_limiter.reserve("tenant-a", 100)

    assert read_rows(db_path) == [("tenant-a", 100)]


async def test_failed_commit_rolls_back_and_recovers(small_limiter, db_path):
    connection = small_limiter._db
    small_limiter._db = Interceptor(connection, "COMMIT", "fail")

    with pytest.raises(sqlite3.OperationalError):
        await small_limiter.reserve("tenant-a", 100)

    assert not connection.in_transaction
    assert read_rows(db_path) == []
    assert await small_limiter.reserve("tenant-a", 100) is not None


async def test_locked_database_fails_reservation_and_recovers(impatient_limiter, db_path):
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.OperationalError):
        await impatient_limiter.reserve("tenant-a", 100)

    assert not impatient_limiter._db.in_transaction
    blocker.execute("ROLLBACK")
    blocker.close()
    assert await impatient_limiter.reserve("tenant-a", 100) is not None
    assert read_rows(db_path) == [("tenant-a", 100)]


async def test_locked_database_fails_settlement_and_keeps_reservation(impatient_limiter, db_path):
    reservation_id = await impatient_limiter.reserve("tenant-a", 100)
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.OperationalError):
        await impatient_limiter.settle(reservation_id, 10)

    blocker.execute("ROLLBACK")
    blocker.close()
    assert read_rows(db_path) == [("tenant-a", 100)]
    await impatient_limiter.settle(reservation_id, 10)
    assert read_rows(db_path) == [("tenant-a", 10)]


async def test_settlement_on_closed_connection_raises_value_error(db_path, clock):
    instance = TokenRateLimiter(db_path)
    await instance.open()
    reservation_id = await instance.reserve("tenant-a", 100)
    await instance.close()

    with pytest.raises(ValueError):
        await instance.settle(reservation_id, 10)
