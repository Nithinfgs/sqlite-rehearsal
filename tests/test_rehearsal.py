import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from sqlite_rehearsal import RehearsalError, rehearse
from sqlite_rehearsal.cli import main


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'source #1.db'
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT); INSERT INTO tasks VALUES(1,'keep'),(2,'done');")

    def sql(self, text, name='migration.sql'):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_schema_diff_source_preservation(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        r = rehearse(self.db, [self.sql('ALTER TABLE tasks ADD COLUMN status TEXT; CREATE INDEX ix ON tasks(status);')])
        self.assertTrue(r['ok'])
        self.assertEqual(r['steps'][0]['changes']['added'][0]['name'], 'ix')
        self.assertEqual(r['steps'][0]['changes']['changed'][0]['after']['name'], 'tasks')
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_row_loss_opt_in(self):
        sql = self.sql('DELETE FROM tasks WHERE id=2;')
        self.assertTrue(rehearse(self.db, [sql])['ok'])
        r = rehearse(self.db, [sql], fail_on_row_loss=True)
        self.assertFalse(r['ok'])
        self.assertEqual(r['steps'][0]['changes']['rows'][0]['delta'], -1)

    def test_sqlite_prefix_user_table_included(self):
        r = rehearse(self.db, [self.sql('CREATE TABLE sqliteXdata(x); INSERT INTO sqliteXdata VALUES(1);')])
        self.assertTrue(r['ok'])
        self.assertEqual(r['steps'][0]['changes']['rows'][0]['table'], 'sqliteXdata')

    def test_drop_table(self):
        r = rehearse(self.db, [self.sql('DROP TABLE tasks;')], fail_on_row_loss=True)
        self.assertFalse(r['ok'])
        self.assertEqual(r['steps'][0]['changes']['rows'][0]['delta'], -2)

    def test_order_stop_on_error(self):
        paths = [self.sql('ALTER TABLE tasks ADD COLUMN n INT;', '1.sql'), self.sql('INSERT INTO nonexistent VALUES(1);', '2.sql'), self.sql('DROP TABLE tasks;', '3.sql')]
        r = rehearse(self.db, paths)
        self.assertFalse(r['ok'])
        self.assertEqual(len(r['steps']), 2)
        self.assertIn('nonexistent', r['steps'][1]['error'])

    def test_constraint(self):
        r = rehearse(self.db, [self.sql('INSERT INTO tasks(id) VALUES(1);')])
        self.assertFalse(r['ok'])
        self.assertIn('UNIQUE', r['steps'][0]['error'])

    def test_foreign_key_enforcement(self):
        r = rehearse(self.db, [self.sql('CREATE TABLE child(id REFERENCES tasks(id)); INSERT INTO child VALUES(99);')])
        self.assertFalse(r['ok'])
        self.assertIn('FOREIGN KEY', r['steps'][0]['error'])

    def test_disabled_foreign_keys_checked(self):
        r = rehearse(self.db, [self.sql('PRAGMA foreign_keys=OFF; CREATE TABLE child(id REFERENCES tasks(id)); INSERT INTO child VALUES(99);')])
        self.assertFalse(r['ok'])
        self.assertEqual(r['steps'][0]['foreign_keys'][0][0], 'child')

    def test_baseline_violations(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('CREATE TABLE child(id REFERENCES tasks(id)); INSERT INTO child VALUES(99);')
        r = rehearse(self.db, [self.sql('SELECT 1;')])
        self.assertFalse(r['ok'])
        self.assertIn('baseline', r['issues'][0])

    def test_quoted_names_triggers(self):
        sql = '''CREATE TABLE "a""b"(x TEXT);
        CREATE TRIGGER add_log AFTER INSERT ON tasks BEGIN
          INSERT INTO "a""b" VALUES ('semi;colon');
          INSERT INTO "a""b" VALUES ('second');
        END;
        INSERT INTO tasks VALUES(3, 'third');'''
        r = rehearse(self.db, [self.sql(sql)])
        self.assertTrue(r['ok'])
        self.assertEqual({x['table']: x['after'] for x in r['steps'][0]['changes']['rows']}['a"b'], 2)

    def test_transaction(self):
        self.assertTrue(rehearse(self.db, [self.sql("BEGIN; UPDATE tasks SET title='x'; COMMIT;")])['ok'])

    def test_unclosed_transaction(self):
        r = rehearse(self.db, [self.sql('BEGIN; DELETE FROM tasks;')])
        self.assertFalse(r['ok'])
        self.assertIn('open transaction', r['steps'][0]['error'])

    def test_wal_data(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute("INSERT INTO tasks VALUES(3, 'wal')")
        conn.commit()
        self.assertTrue(Path(str(self.db) + '-wal').exists())
        r = rehearse(self.db, [self.sql('SELECT 1;')])
        self.assertEqual(r['baseline']['rows']['tasks'], 3)
        self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 3)

    def test_attach_vacuum_blocked(self):
        target = self.root / 'escaped.db'
        for sql in (f"ATTACH '{target.as_posix()}' AS other;", f"VACUUM INTO '{target.as_posix()}';"):
            with self.subTest(sql=sql):
                self.assertFalse(rehearse(self.db, [self.sql(sql)])['ok'])
                self.assertFalse(target.exists())

    def test_dangerous_pragmas_extensions(self):
        for sql in ('PRAGMA writable_schema=ON;', "SELECT load_extension('missing');", "PRAGMA temp_store_directory='/tmp';"):
            with self.subTest(sql=sql):
                self.assertFalse(rehearse(self.db, [self.sql(sql)])['ok'])

    def test_timeout(self):
        r = rehearse(self.db, [self.sql('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x;')], timeout=0.1)
        self.assertFalse(r['ok'])
        self.assertIn('interrupted', r['steps'][0]['error'])

    def test_missing_database(self):
        path = self.root / 'absent.db'
        with self.assertRaises(RehearsalError):
            rehearse(path, [self.sql('SELECT 1;')])
        self.assertFalse(path.exists())

    def test_bad_database(self):
        self.db.write_bytes(b'not sqlite')
        with self.assertRaises(RehearsalError):
            rehearse(self.db, [self.sql('SELECT 1;')])

    def test_invalid_inputs(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(RehearsalError):
                rehearse(self.db, [self.sql('SELECT 1;')], timeout=timeout)
        with self.assertRaises(RehearsalError):
            rehearse(self.db, [])
        with self.assertRaises(RehearsalError):
            rehearse(self.db, [self.root / 'missing.sql'])

    def test_cli_json(self):
        for sql, expected in [('SELECT 1;', 0), ('NOT SQL;', 1)]:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main([str(self.db), str(self.sql(sql)), '--json'])
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(out.getvalue())['ok'], expected == 0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main([str(self.root / 'absent'), 'missing', '--json'])
        self.assertEqual(code, 2)
        self.assertIn('error', json.loads(out.getvalue()))

    def test_cli_text(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main([str(self.db), str(self.sql('DELETE FROM tasks;')), '--fail-on-row-loss'])
        self.assertEqual(code, 1)
        self.assertIn('2 -> 0 (-2)', out.getvalue())


if __name__ == '__main__':
    unittest.main()
