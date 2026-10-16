# Isolation lab

Run two `psql` sessions against the compose database.

## Read Committed

Session A:

```sql
BEGIN;
SELECT balance_paise FROM accounts WHERE id = 1;
SELECT balance_paise FROM accounts WHERE id = 1;
COMMIT;
```

Session B, between A's reads:

```sql
BEGIN;
UPDATE accounts SET balance_paise = balance_paise + 1 WHERE id = 1;
COMMIT;
```

The second A read sees B's committed value because each statement gets a new snapshot.

## Repeatable Read

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT balance_paise FROM accounts WHERE id = 1;
-- Session B updates and commits.
SELECT balance_paise FROM accounts WHERE id = 1;
COMMIT;
```

A sees the original value in both reads. A join uses the same transaction snapshot and has the same guarantee.

## Serializable

Two sessions each run `BEGIN ISOLATION LEVEL SERIALIZABLE`, read the same balance, and write a decision based on it. At commit, PostgreSQL aborts one with `could not serialize access due to concurrent update` or a serialization failure. The caller must retry the whole transaction.

The transfer implementation uses Read Committed with account row locks, a deterministic lock order, and a balance reread under lock.
