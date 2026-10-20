# SQLite Rehearsal

Run SQL migrations against a disposable copy of your SQLite database. See the schema changes, table row-count changes, foreign-key violations, and integrity results before applying the migration yourself.

Python 3.10+, no runtime dependencies. Works offline. No database contents leave your machine.

## Quick start

```sh
git clone https://github.com/Nithinfgs/sqlite-rehearsal.git
cd sqlite-rehearsal
python -m venv .venv
# macOS/Linux:
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install .
python examples/create_demo.py
sqlite-rehearsal demo.db examples/001_add_status.sql
```

Example output (duration varies):

```text
PASS
  OK examples/001_add_status.sql (0.4 ms)
    added: index tasks_status_idx
    changed: table tasks
```

Catch a deletion before you apply it:

```sh
sqlite-rehearsal demo.db examples/001_add_status.sql examples/002_remove_done.sql --fail-on-row-loss
```

This exits with status 1 and reports `rows tasks: 2 -> 1 (-1)`. The original `demo.db` still has two rows and no `status` column.

You can also run directly from a checkout using `python -m sqlite_rehearsal`.

## Options and CI

```sh
sqlite-rehearsal app.db migrations/001.sql migrations/002.sql --json --timeout 60 > report.json
```

Files execute in the supplied order; directories and migration discovery are deliberately not implicit. Each SQL file must end outside a transaction. `BEGIN`/`COMMIT` within a file and multi-statement triggers are supported. UTF-8 files, including a BOM, are accepted.

| Option | Behavior |
| --- | --- |
| `--json` | Structured report on stdout, including errors |
| `--timeout SECONDS` | Positive cooperative SQLite execution/backup deadline; default 30 |
| `--fail-on-row-loss` | Fail if any table's row count decreases after a migration, including dropped tables |

Exit codes: **0** all checks passed; **1** migration, integrity, foreign-key, or configured row-loss check failed; **2** invalid input, backup, or inspection failure. Argument parsing errors also return 2 through argparse.

A report has `format_version`, `ok`, `issues`, a `baseline` snapshot, and ordered `steps`. Each successful SQL execution includes `changes` (`added`, `removed`, `changed`, `rows`), `foreign_keys`, and `integrity`. SQL failures include `error`; subsequent migrations are not run. Existing baseline violations cause an overall failure even if a migration repairs them. SQL objects are compared using SQLite's stored definitions. Reports include schema definitions, filenames, and violation row identifiers; treat them as potentially sensitive.

For CI, provision a representative test database first, then run the same CLI with `--json --fail-on-row-loss`. This repository's workflow tests installation and behavior across Linux, macOS, Windows and Python 3.10/3.13.

## How it works

1. Opens the existing source using SQLite `mode=ro`.
2. Uses the [SQLite backup API](https://sqlite.org/backup.html) to obtain a consistent snapshot, including committed WAL contents, in a temporary directory.
3. Records schema, row counts, `integrity_check`, and `foreign_key_check`.
4. Executes each file on the snapshot, then repeats inspection and computes differences.
5. Closes connections and removes the disposable snapshot, including on errors.

Foreign-key enforcement starts enabled. Scripts may disable it for table-rebuild migrations, but an explicit foreign-key check still runs after every file. Migrations must restore consistency at each file boundary. Extension loading, `ATTACH`/`DETACH`, `VACUUM INTO`, and unsupported pragmas are blocked by a SQLite authorizer. Supported pragmas are listed in `authorize()` in `core.py`; ordinary `VACUUM` is also unsupported because SQLite internally attaches a database.

## Scope and limitations

- This is a rehearsal tool, not a migration manager or apply command. It never applies SQL to the original database.
- Row counts cannot detect same-count replacements, changed values, or logical application errors. A table rename appears as removal/addition and can trigger row-loss failure. Use application tests for data correctness.
- Runtime depends on database size: the snapshot requires disk space, and full counts/integrity checks scan data. The timeout cooperatively interrupts SQLite work; it is not a hard wall-clock or memory limit, and file reads occur before the deadline starts.
- The SQL authorizer is defense in depth, not an OS security sandbox. Use trusted databases and migrations; custom functions, extensions, encrypted databases, and application-specific virtual table modules are not provided.
- WAL readers may require/create SQLite sidecar files according to SQLite's normal behavior. Source data is opened read-only; this is not a guarantee of zero filesystem metadata changes.
- A passing rehearsal applies only to the captured snapshot and bundled SQLite version, not to a later production state or another database engine. Timings are informational, not performance predictions.
- No rollback rehearsal, query-plan comparison, data-value diff, or migration-file tracking in version 0.1.

## Python API

```python
from sqlite_rehearsal import rehearse, RehearsalError

report = rehearse('app.db', ['001.sql'], fail_on_row_loss=True, timeout=60)
print(report['ok'])
```

`RehearsalError` covers invalid inputs and snapshot/inspection failures. SQL failures are returned as structured reports.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests cover source preservation, WAL snapshots, row loss, trigger parsing, constraints, foreign-key checks, transaction boundaries, filesystem escape prevention, timeout, input errors, and CLI output/exit codes. Contributions should include a reproducing test and avoid adding runtime dependencies without a clear need.

## License

MIT. Original implementation; uses Python's standard-library SQLite bindings.
