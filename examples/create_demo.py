"""Create demo.db without overwriting an existing file."""
import sqlite3
from contextlib import closing
from pathlib import Path

path = Path('demo.db')
with path.open('xb'):
    pass
with closing(sqlite3.connect(path)) as db:
    db.executescript("CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT NOT NULL);"
                     "INSERT INTO tasks(title) VALUES ('write tests'), ('done');")
print('Created demo.db with two tasks.')
