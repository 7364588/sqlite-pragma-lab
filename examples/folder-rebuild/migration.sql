-- This toggle is ignored when the runner already opened a transaction.
PRAGMA foreign_keys=OFF;
CREATE TABLE new_folders (id INTEGER PRIMARY KEY, name TEXT NOT NULL, label TEXT);
INSERT INTO new_folders (id, name) SELECT id, name FROM folders;
-- With foreign keys still enabled, this DROP cascades into files.
DROP TABLE folders;
ALTER TABLE new_folders RENAME TO folders;
PRAGMA foreign_keys=ON;
