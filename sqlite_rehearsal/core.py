"""Rehearse ordered SQL migrations on a disposable SQLite snapshot."""
from __future__ import annotations

import math
import sqlite3
import tempfile
import time
from pathlib import Path


class RehearsalError(Exception):
    """Invalid input or failure to obtain/inspect a snapshot."""


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def snapshot(db: sqlite3.Connection) -> dict:
    schema = {
        f'{kind}:{name}': {'type': kind, 'name': name, 'table': table, 'sql': sql}
        for kind, name, table, sql in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE substr(name,1,7) != 'sqlite_' ORDER BY type,name"
        )
    }
    counts = {
        entry['name']: db.execute(f'SELECT count(*) FROM {quote(entry["name"])}').fetchone()[0]
        for entry in schema.values() if entry['type'] == 'table'
    }
    violations = [list(row) for row in db.execute('PRAGMA foreign_key_check')]
    integrity = [row[0] for row in db.execute('PRAGMA integrity_check')]
    return {'schema': schema, 'rows': counts, 'foreign_keys': violations, 'integrity': integrity}


def difference(before: dict, after: dict) -> dict:
    old, new = before['schema'], after['schema']
    return {
        'added': [new[k] for k in sorted(new.keys() - old.keys())],
        'removed': [old[k] for k in sorted(old.keys() - new.keys())],
        'changed': [{'before': old[k], 'after': new[k]} for k in sorted(old.keys() & new.keys()) if old[k] != new[k]],
        'rows': [
            {'table': table, 'before': before['rows'].get(table, 0),
             'after': after['rows'].get(table, 0),
             'delta': after['rows'].get(table, 0) - before['rows'].get(table, 0)}
            for table in sorted(before['rows'].keys() | after['rows'].keys())
            if before['rows'].get(table) != after['rows'].get(table)
        ],
    }


def authorize(action, arg1, arg2, database, trigger):
    # ATTACH and VACUUM INTO can write outside the disposable database.
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in ('load_extension', 'writefile', 'readfile'):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        # Allow common migration pragmas; reject file-path and schema-corruption knobs.
        allowed = {'foreign_keys', 'defer_foreign_keys', 'user_version', 'application_id',
                   'table_info', 'table_xinfo', 'index_list', 'index_info', 'index_xinfo',
                   'foreign_key_list', 'foreign_key_check', 'integrity_check', 'quick_check'}
        if (arg1 or '').lower() not in allowed:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def rehearse(database: Path | str, migrations: list[Path | str], *, timeout: float = 30,
             fail_on_row_loss: bool = False) -> dict:
    """Never apply migrations to the input; return JSON-serializable evidence.

    Timeout is a cooperative SQLite execution/backup deadline, not an OS sandbox.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise RehearsalError('timeout must be a finite positive number')
    path = Path(database).resolve()
    if not path.is_file():
        raise RehearsalError(f'database is not an existing file: {path}')
    if not migrations:
        raise RehearsalError('provide at least one migration')
    try:
        scripts = [(str(Path(p)), Path(p).read_text(encoding='utf-8-sig')) for p in migrations]
    except (OSError, UnicodeError) as exc:
        raise RehearsalError(str(exc)) from exc
    deadline = time.monotonic() + timeout

    def progress():
        return int(time.monotonic() > deadline)

    def backup_progress(status, remaining, total):
        if progress():
            raise RehearsalError('snapshot backup timed out')

    try:
        with tempfile.TemporaryDirectory(prefix='sqlite-rehearsal-') as temp:
            source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=min(timeout, 5))
            db = sqlite3.connect(str(Path(temp) / 'snapshot.db'), isolation_level=None)
            try:
                source.backup(db, pages=256, progress=backup_progress, sleep=0.01)
            except BaseException:
                db.close()
                raise
            finally:
                source.close()
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA trusted_schema=OFF')
                db.set_progress_handler(progress, 1000)
                baseline = snapshot(db)
                report = {'format_version': 1, 'database': str(path), 'sqlite_version': sqlite3.sqlite_version,
                          'baseline': baseline, 'steps': [], 'ok': True, 'issues': []}
                if baseline['foreign_keys'] or baseline['integrity'] != ['ok']:
                    report['issues'].append('baseline database fails integrity or foreign-key checks')
                previous = baseline
                for name, sql in scripts:
                    start = time.monotonic()
                    step = {'migration': name, 'ok': False}
                    db.set_authorizer(authorize)
                    try:
                        db.executescript(sql)
                        if db.in_transaction:
                            raise sqlite3.OperationalError('migration leaves an open transaction; add COMMIT or ROLLBACK')
                    except sqlite3.Error as exc:
                        step['error'] = str(exc)
                        report['issues'].append(f'migration failed: {name}')
                    step['duration_ms'] = round((time.monotonic() - start) * 1000, 3)
                    if 'error' in step:
                        report['steps'].append(step)
                        break
                    after = snapshot(db)
                    step.update(ok=True, changes=difference(previous, after),
                                foreign_keys=after['foreign_keys'], integrity=after['integrity'])
                    if after['foreign_keys'] or after['integrity'] != ['ok']:
                        step['ok'] = False
                        report['issues'].append(f'validation failed after: {name}')
                    if fail_on_row_loss and any(row['delta'] < 0 for row in step['changes']['rows']):
                        step['ok'] = False
                        report['issues'].append(f'row count decreased after: {name}')
                    report['steps'].append(step)
                    previous = after
                    if not step['ok']:
                        break
                report['ok'] = not report['issues']
                return report
            finally:
                db.close()
    except (sqlite3.Error, OSError) as exc:
        raise RehearsalError(str(exc)) from exc
