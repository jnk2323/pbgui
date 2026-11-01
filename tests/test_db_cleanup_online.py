"""Online cleanup uses isolated SQLite data and never controls real services."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import db_maintenance as maintenance
from database_lock import DatabaseBusyError, acquire_database_lock, recovery_record
from test_db_maintenance import tools, _request


def _rows(tools, root):
    """Populate every handled table with exact and similarly named users."""
    paths = {name: root / "data" / name for name in maintenance.DB_NAMES}
    paths["pbgui.db"].parent.mkdir(parents=True, exist_ok=True)
    for spec in tools.TABLE_SPECS:
        with closing(sqlite3.connect(paths[spec.db_name])) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f'CREATE TABLE "{spec.table}" (user TEXT, "{spec.timestamp_col}" INTEGER)')
            conn.executemany(f'INSERT INTO "{spec.table}" VALUES (?, ?)',
                             [(user, timestamp) for user in ("binance_CPT", "binance_CPT2", "other")
                              for timestamp in (100, 200)])
            conn.commit()
    return paths


@pytest.mark.parametrize("cutoff,remaining", [(None, 0), (150, 1)])
def test_cleanup_coexists_with_scan_and_keeps_other_users(tmp_path, tools, cutoff, remaining):
    """An existing scan lease permits exact-user deletion from every table."""
    paths = _rows(tools, tmp_path)
    with acquire_database_lock(tmp_path):
        maintenance.probe(tmp_path, online_cleanup=True)
        result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"], cutoff_ms=cutoff))
    assert result["deleted"]["total"] == len(tools.TABLE_SPECS) * (2 - remaining)
    assert tools.control.events == []
    assert recovery_record(tmp_path) is None
    for spec in tools.TABLE_SPECS:
        with closing(sqlite3.connect(paths[spec.db_name])) as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM "{spec.table}" WHERE user=?', ("binance_CPT",)).fetchone()[0] == remaining
            assert conn.execute(f'SELECT COUNT(*) FROM "{spec.table}" WHERE user!=?', ("binance_CPT",)).fetchone()[0] == 4
    assert len(result["backups"]) == 2
    for raw in result["backups"]:
        backup = Path(raw)
        spec = next(spec for spec in tools.TABLE_SPECS if backup.name.endswith(spec.db_name))
        with closing(sqlite3.connect(backup)) as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM "{spec.table}" WHERE user=?', ("binance_CPT",)).fetchone()[0] == 2
        assert backup.stat().st_mode & 0o777 == 0o600


def test_cleanup_waits_for_sqlite_writer_without_stopping_pbdata(tmp_path, tools, monkeypatch):
    """A concurrent short transaction completes before cleanup acquires its writer."""
    paths = _rows(tools, tmp_path)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def writer():
        """Hold a real write transaction until cleanup reaches BEGIN IMMEDIATE."""
        try:
            with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO balances VALUES ('new-user', 300)")
                entered.set()
                assert release.wait(5)
                conn.commit()
        except BaseException as exc:
            errors.append(exc)

    original = tools._connect_bundle

    def connect(paths):
        """Release the writer only when cleanup requests SQLite write access."""
        connections = original(paths)
        connections["pbgui.db"].set_trace_callback(lambda sql: release.set() if sql == "BEGIN IMMEDIATE" else None)
        return connections

    monkeypatch.setattr(tools, "_connect_bundle", connect)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(5)
        result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
        assert result["ok"]
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert tools.control.events == []
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM balances WHERE user='new-user'").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["sql", "cancel", "backup"])
def test_online_failure_never_restores_over_live_data(tmp_path, tools, monkeypatch, failure):
    """Failures retain confirmed batches, roll back open ones and keep live writes."""
    paths = _rows(tools, tmp_path)
    cancelled = threading.Event()
    original = maintenance.backup_sqlite_database

    def backup(source, destination, **kwargs):
        """Insert another user's data after its backup, before any deletion."""
        original(source, destination, **kwargs)
        if source.name == "pbgui.db":
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("INSERT INTO balances VALUES ('new-user', 300)")
                conn.commit()
        elif failure == "backup":
            raise OSError("backup failed")

    def advance(label, *args):
        """Cancel after DELETE has changed uncommitted rows in the first DB."""
        if failure == "cancel" and label.startswith("Deleted rows"):
            cancelled.set()

    if failure == "sql":
        with closing(sqlite3.connect(paths["pbgui_trades.db"])) as conn:
            conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON executions BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            conn.commit()
    monkeypatch.setattr(maintenance, "backup_sqlite_database", backup)
    restore = Mock(side_effect=AssertionError("Online cleanup must never restore entire databases"))
    monkeypatch.setattr(maintenance, "restore_sqlite_backup", restore)
    progress = SimpleNamespace(set_current=lambda *args: None, advance=advance)
    if failure == "cancel":
        result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]), cancel=cancelled, progress=progress)
        assert result["cancelled"]
        assert result["deleted"]["total"] == 2
    else:
        with pytest.raises(OSError if failure == "backup" else RuntimeError):
            maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]), cancel=cancelled, progress=progress)
    for spec in tools.TABLE_SPECS:
        with closing(sqlite3.connect(paths[spec.db_name])) as conn:
            expected = 2 if (failure == "backup" or spec.db_name == "pbgui_trades.db"
                             or (failure == "cancel" and spec.table != "history")) else 0
            assert conn.execute(f'SELECT COUNT(*) FROM "{spec.table}" WHERE user=?', ("binance_CPT",)).fetchone()[0] == expected
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM balances WHERE user='new-user'").fetchone()[0] == 1
    assert recovery_record(tmp_path) is None
    assert tools.control.events == []
    restore.assert_not_called()
    if failure == "backup":
        assert not list((tmp_path / "data" / "backup" / "db-tools").glob("*.db"))
    else:
        assert len(list((tmp_path / "data" / "backup" / "db-tools").glob("*.db"))) == 2
    with acquire_database_lock(tmp_path, exclusive=True):
        pass


def test_cleanup_route_accepts_shared_scan_lease(tmp_path, tools, monkeypatch):
    """The API preflight must not reject a user just because PBData is scanning."""
    monkeypatch.setattr(tools, "_operation_total", lambda *args: 9, raising=False)
    payload = SimpleNamespace(target="local", users=["binance_CPT"], mode="all", cutoff_ms=None)
    with acquire_database_lock(tmp_path):
        asyncio.run(tools.cleanup_run(payload, session=None))
    tools._start_operation.assert_called_once()


def test_online_owner_excludes_restore_and_rejects_released_owner(tmp_path, tools):
    """Shared cleanup ownership still excludes file maintenance and stale bypasses."""
    paths = _rows(tools, tmp_path)
    with maintenance.OnlineCleanup(tmp_path, "a" * 32) as owner:
        with pytest.raises(DatabaseBusyError):
            acquire_database_lock(tmp_path, exclusive=True)
        with acquire_database_lock(tmp_path):
            pass
    with pytest.raises(DatabaseBusyError, match="active target owner"):
        tools.delete_user_rows(paths, ["binance_CPT"], _maintenance=owner)


def test_busy_second_database_reports_completed_batches(tmp_path, tools, monkeypatch):
    """A persistent second writer reports committed work in the first database."""
    paths = _rows(tools, tmp_path)
    original = tools._connect_bundle

    def connect(paths):
        """Use a short isolated-test wait instead of the production 30 seconds."""
        connections = original(paths)
        for conn in connections.values():
            conn.execute("PRAGMA busy_timeout=1")
        return connections

    monkeypatch.setattr(tools, "_connect_bundle", connect)
    with closing(sqlite3.connect(paths["pbgui_trades.db"])) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="12 committed rows"):
            maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
        writer.rollback()
    for spec in tools.TABLE_SPECS:
        with closing(sqlite3.connect(paths[spec.db_name])) as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM "{spec.table}" WHERE user=?', ("binance_CPT",)).fetchone()[0] == (2 if spec.db_name == "pbgui_trades.db" else 0)
    assert tools.control.events == []


def test_second_commit_failure_is_reported_and_retry_finishes(tmp_path, tools, monkeypatch):
    """A failed cross-DB commit reports its boundary without restoring other users."""
    paths = _rows(tools, tmp_path)
    original = tools._connect_bundle

    class FailCommit:
        """Delegate real SQL except for the simulated second-database disk failure."""

        def __init__(self, connection):
            """Retain the real connection for rollback and close."""
            self.connection = connection

        def __getattr__(self, name):
            """Forward all other database operations unchanged."""
            return getattr(self.connection, name)

        def commit(self):
            """Simulate failure after the first database's successful commit."""
            raise sqlite3.OperationalError("simulated commit failure")

    def connect(paths):
        """Inject the failure only after online backups have completed."""
        connections = original(paths)
        connections["pbgui_trades.db"] = FailCommit(connections["pbgui_trades.db"])
        return connections

    monkeypatch.setattr(tools, "_connect_bundle", connect)
    with pytest.raises(RuntimeError, match="12 committed rows.*Retry cleanup"):
        maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
    for name, expected in (("pbgui.db", 0), ("pbgui_trades.db", 2)):
        table = "history" if name == "pbgui.db" else "executions"
        with closing(sqlite3.connect(paths[name])) as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE user=?', ("binance_CPT",)).fetchone()[0] == expected
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE user!=?', ("binance_CPT",)).fetchone()[0] == 4
    monkeypatch.setattr(tools, "_connect_bundle", original)
    result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
    assert result["deleted"]["total"] == 2
    assert tools.control.events == []


def test_cleanup_does_not_create_missing_database(tmp_path, tools):
    """Removing an account from a partial installation does not create empty DBs."""
    paths = _rows(tools, tmp_path)
    paths["pbgui_trades.db"].unlink()
    result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
    assert result["deleted"]["total"] == len(tools.MAIN_TABLES) * 2
    assert not paths["pbgui_trades.db"].exists()
    assert len(result["backups"]) == 1


def test_changes_after_backup_are_preserved(tmp_path, tools, monkeypatch):
    """New rows, updated rows and reused rowids are never deleted unbacked-up."""
    paths = _rows(tools, tmp_path)
    original = maintenance.backup_sqlite_database

    def backup(source, destination, **kwargs):
        """Change the selected account after the full snapshot has been saved."""
        original(source, destination, **kwargs)
        if source.name == "pbgui.db":
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("UPDATE history SET timestamp=999 WHERE rowid=1")
                conn.execute("DELETE FROM history WHERE rowid=2")
                conn.execute("INSERT INTO history(rowid,user,timestamp) VALUES (2,'binance_CPT',222)")
                conn.execute("INSERT INTO history VALUES ('binance_CPT',300)")
                conn.commit()

    monkeypatch.setattr(maintenance, "backup_sqlite_database", backup)
    result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
    assert result["deleted"]["total"] == 12
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert sorted(row[0] for row in conn.execute("SELECT timestamp FROM history WHERE user='binance_CPT'")) == [222, 300, 999]


def test_cancel_before_batch_commit_rolls_back_batch(tmp_path, tools, monkeypatch):
    """Cancellation before COMMIT returns zero deletions and retains every row."""
    paths = _rows(tools, tmp_path)
    cancelled = threading.Event()
    original = tools._connect_bundle

    def connect(paths):
        """Signal cancellation during the first batch's DELETE statements."""
        connections = original(paths)
        connections["pbgui.db"].set_trace_callback(lambda sql: cancelled.set() if sql.startswith("DELETE") else None)
        return connections

    monkeypatch.setattr(tools, "_connect_bundle", connect)
    result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]), cancel=cancelled)
    assert result["cancelled"]
    assert result["deleted"]["total"] == 0
    assert result["backups"] == []
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 6


@pytest.mark.parametrize("cutoff,remaining", [(None, 0), (150, 1)])
def test_updated_scan_checkpoint_is_reset_only_for_full_cleanup(tmp_path, tools, monkeypatch, cutoff, remaining):
    """A changed scan timestamp cannot survive full history cleanup as stale state."""
    paths = _rows(tools, tmp_path)
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        conn.execute("DROP TABLE history_scan_meta")
        conn.execute("CREATE TABLE history_scan_meta(user TEXT, exchange TEXT, last_scan_ts INTEGER, PRIMARY KEY(user,exchange))")
        conn.executemany("INSERT INTO history_scan_meta VALUES (?, 'mock', 100)", [("binance_CPT",), ("other",)])
        conn.commit()
    original = maintenance.backup_sqlite_database

    def backup(source, destination, **kwargs):
        """Advance the real composite-key checkpoint after its snapshot."""
        original(source, destination, **kwargs)
        if source.name == "pbgui.db":
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("UPDATE history_scan_meta SET last_scan_ts=1000 WHERE user='binance_CPT'")
                conn.commit()

    monkeypatch.setattr(maintenance, "backup_sqlite_database", backup)
    maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"], cutoff_ms=cutoff))
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_scan_meta WHERE user='binance_CPT'").fetchone()[0] == remaining
        assert conn.execute("SELECT COUNT(*) FROM history_scan_meta WHERE user='other'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM history WHERE user='binance_CPT'").fetchone()[0] == (0 if cutoff is None else 1)


@pytest.mark.parametrize("failure", ["lock", "open"])
def test_repeated_zero_deletion_failures_remove_only_their_backups(tmp_path, tools, monkeypatch, failure):
    """Repeated first-batch/open failures cannot accumulate full database copies."""
    paths = _rows(tools, tmp_path)
    directory = tmp_path / "data" / "backup" / "db-tools"
    directory.mkdir(parents=True)
    previous = directory / "previous-pbgui.db"
    previous.write_bytes(b"retained earlier backup")
    original = tools._connect_bundle

    def connect(paths):
        """Fail before any deletion or use an immediate busy timeout."""
        if failure == "open":
            raise sqlite3.OperationalError("injected open failure")
        connections = original(paths)
        for conn in connections.values():
            conn.execute("PRAGMA busy_timeout=1")
        return connections

    monkeypatch.setattr(tools, "_connect_bundle", connect)
    with closing(sqlite3.connect(paths["pbgui.db"])) as writer:
        writer.execute("BEGIN IMMEDIATE")
        for _ in range(2):
            with pytest.raises((RuntimeError, sqlite3.OperationalError)) as error:
                maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]))
            assert "backups were retained" not in str(error.value)
            assert list(directory.iterdir()) == [previous]
        writer.rollback()
    assert previous.read_bytes() == b"retained earlier backup"
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM history WHERE user='binance_CPT'").fetchone()[0] == 2


def test_late_api_cancellation_preserves_successful_result(tmp_path, tools, monkeypatch):
    """The wrapper must not call committed cleanup an API shutdown failure."""
    entered, release = threading.Event(), threading.Event()
    expected = {"ok": True, "cancelled": False, "deleted": {"total": 42}}

    def run(*args, **kwargs):
        """Model a completed commit while delivery of its result is delayed."""
        entered.set()
        assert release.wait(5)
        return expected

    monkeypatch.setattr(maintenance, "run", run)

    async def exercise():
        """Cancel the request after the worker has committed, then drain it."""
        task = asyncio.create_task(tools._maintain_target("local", {"kind": "cleanup", "users": ["binance_CPT"]}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        assert await task == expected

    asyncio.run(exercise())


def test_large_cleanup_allows_continuous_writer(tmp_path, tools):
    """Clean a 64 MiB indexed table while a second connection keeps committing."""
    paths = _rows(tools, tmp_path)
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        conn.execute("ALTER TABLE history ADD COLUMN payload BLOB")
        conn.execute("CREATE INDEX history_user_ts ON history(user,timestamp)")
        conn.executemany("INSERT INTO history(user,timestamp,payload) VALUES ('binance_CPT',?,zeroblob(8192))",
                         ((value,) for value in range(8192)))
        conn.commit()
    stop, started = threading.Event(), threading.Event()
    errors, commits = [], []

    def writer():
        """Commit unrelated updates frequently throughout backup and deletion."""
        try:
            with closing(sqlite3.connect(paths["pbgui.db"], timeout=2)) as conn:
                while not stop.is_set():
                    conn.execute("UPDATE balances SET timestamp=timestamp+1 WHERE user='other'")
                    conn.commit()
                    commits.append(1)
                    started.set()
                    stop.wait(0.005)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    checkpoints, history_checkpoints = [], []

    def progress(*args):
        """Record writer progress at operation checkpoints."""
        checkpoints.append(len(commits))
        if "pbgui.db:history" in args[0]:
            history_checkpoints.append(len(commits))

    try:
        assert started.wait(5)
        result = maintenance.run(tmp_path, _request({}, kind="cleanup", users=["binance_CPT"]),
                                 progress=SimpleNamespace(set_current=progress, advance=progress))
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert result["deleted"]["total"] == 8192 + 14
    assert checkpoints[-1] > checkpoints[0] + 5
    assert history_checkpoints[-1] > history_checkpoints[0] + 5
    with closing(sqlite3.connect(paths["pbgui.db"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM history WHERE user='binance_CPT'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM history WHERE user!='binance_CPT'").fetchone()[0] == 4
    assert tools.control.events == []


@pytest.mark.parametrize("preflight", [True, False])
def test_old_remote_has_clear_online_cleanup_error(tmp_path, tools, monkeypatch, preflight):
    """Both remote entry points translate a missing cleanup capability clearly."""
    import builtins
    from types import ModuleType
    from fastapi import HTTPException

    old_module = ModuleType("db_maintenance")
    old_module.check_remote_capability = lambda: None
    old_module.probe = lambda *args, **kwargs: None
    original_import = builtins.__import__

    def old_import(name, *args, **kwargs):
        """Model an old remote module without touching files or real hosts."""
        return old_module if name == "db_maintenance" else original_import(name, *args, **kwargs)

    async def rpc(target, script, args, **kwargs):
        """Run only the generated capability check against the old module."""
        assert "OnlineCleanup" in script
        try:
            exec(script, {"__builtins__": {**vars(builtins), "__import__": old_import}})
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        raise AssertionError("Old remote must not reach mutation")

    monkeypatch.setattr(tools, "_run_remote_python", rpc)
    with pytest.raises(HTTPException) as error:
        if preflight:
            asyncio.run(tools._assert_maintenance_available("old-master", online_cleanup=True))
        else:
            asyncio.run(tools._maintain_target("old-master", {"kind": "cleanup", "users": ["binance_CPT"]}))
    assert error.value.status_code == 409
    assert "Update and restart remote PBGui/PBData" in error.value.detail
    assert "ImportError" not in error.value.detail
