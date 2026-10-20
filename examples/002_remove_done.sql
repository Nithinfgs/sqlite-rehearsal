-- Demonstrates --fail-on-row-loss; this is deliberately destructive on the copy.
DELETE FROM tasks WHERE title = 'done';
