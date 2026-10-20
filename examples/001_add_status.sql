ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
CREATE INDEX tasks_status_idx ON tasks(status);
