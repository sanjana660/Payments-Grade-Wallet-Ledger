CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE kyc_status AS ENUM ('pending', 'verified', 'rejected');
CREATE TYPE account_kind AS ENUM ('wallet', 'cash_in', 'cash_out', 'fee_revenue');
CREATE TYPE transfer_type AS ENUM ('topup', 'peer', 'withdrawal', 'fee', 'reversal');
CREATE TYPE transfer_status AS ENUM ('completed', 'held', 'reversed');
CREATE TYPE entry_direction AS ENUM ('debit', 'credit');
CREATE TYPE idempotency_status AS ENUM ('running', 'completed');

CREATE TABLE users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    kyc_status kyc_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email));
CREATE UNIQUE INDEX users_phone_uq ON users (phone);

CREATE TABLE accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind account_kind NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    balance_paise BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE wallets (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    account_id BIGINT NOT NULL UNIQUE REFERENCES accounts(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX wallets_user_name_uq ON wallets(user_id, lower(name));
CREATE TABLE system_accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL UNIQUE REFERENCES accounts(id),
    name TEXT NOT NULL UNIQUE CHECK (name IN ('cash_in', 'cash_out', 'fee_revenue'))
);

CREATE TABLE transfers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type transfer_type NOT NULL,
    status transfer_status NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    source_account_id BIGINT REFERENCES accounts(id),
    destination_account_id BIGINT REFERENCES accounts(id),
    note TEXT,
    original_transfer_id BIGINT REFERENCES transfers(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX transfers_created_idx ON transfers(created_at DESC, id DESC);
CREATE INDEX transfers_status_idx ON transfers(status, created_at DESC);
CREATE INDEX transfers_source_time_idx ON transfers(source_account_id, created_at DESC);
CREATE INDEX transfers_destination_time_idx ON transfers(destination_account_id, created_at DESC);
CREATE UNIQUE INDEX one_reversal_per_transfer_idx ON transfers(original_transfer_id) WHERE type = 'reversal';

CREATE TABLE ledger_entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id),
    account_id BIGINT NOT NULL REFERENCES accounts(id),
    direction entry_direction NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ledger_account_time_idx ON ledger_entries(account_id, created_at DESC, id DESC);
CREATE INDEX ledger_transfer_idx ON ledger_entries(transfer_id);

CREATE FUNCTION enforce_transfer_balance() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    debit_total BIGINT;
    credit_total BIGINT;
    entry_count INTEGER;
BEGIN
    SELECT count(*), COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0),
                 COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0)
        INTO entry_count, debit_total, credit_total
        FROM ledger_entries WHERE transfer_id = NEW.transfer_id;
    IF entry_count < 2 OR debit_total <> credit_total THEN
        RAISE EXCEPTION 'transfer % is not balanced', NEW.transfer_id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER ledger_entries_balanced
AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_transfer_balance();

CREATE FUNCTION reject_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'ledger entries are immutable'; END;
$$;
CREATE TRIGGER ledger_entries_immutable BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation();

CREATE TABLE idempotency_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    key UUID NOT NULL,
    request_hash CHAR(64) NOT NULL,
    status idempotency_status NOT NULL DEFAULT 'running',
    response_code INTEGER,
    response_body JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, key)
);
CREATE INDEX idempotency_created_idx ON idempotency_keys(created_at);
CREATE UNIQUE INDEX idempotency_global_key_uq ON idempotency_keys(key) WHERE user_id IS NULL;

CREATE TABLE transfer_status_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id),
    old_status transfer_status,
    new_status transfer_status NOT NULL,
    changed_by BIGINT REFERENCES users(id),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_transfer_idx ON transfer_status_audit(transfer_id, changed_at);

CREATE TABLE risk_rules (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT true,
    threshold JSONB NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('hold', 'flag'))
);
CREATE TABLE risk_flags (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id),
    rule_id BIGINT NOT NULL REFERENCES risk_rules(id),
    label TEXT,
    explanation TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(transfer_id, rule_id)
);

CREATE TABLE reconciliation_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    issue_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE reconciliation_issues (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES reconciliation_runs(id),
    check_name TEXT NOT NULL,
    subject_id BIGINT,
    expected TEXT,
    actual TEXT
);
CREATE TABLE job_locks (
    job_name TEXT PRIMARY KEY,
    acquired_at TIMESTAMPTZ
);
INSERT INTO job_locks(job_name) VALUES ('reconciliation');

INSERT INTO accounts(kind) VALUES ('cash_in'), ('cash_out'), ('fee_revenue');
INSERT INTO system_accounts(account_id, name)
SELECT id, CASE kind WHEN 'cash_in' THEN 'cash_in' WHEN 'cash_out' THEN 'cash_out' ELSE 'fee_revenue' END
FROM accounts WHERE kind <> 'wallet';
INSERT INTO risk_rules(name, threshold, action) VALUES
 ('velocity', '{"count": 5, "minutes": 10}', 'hold'),
 ('amount_spike', '{"multiplier": 5, "days": 30}', 'hold'),
 ('new_payee_large', '{"amount_paise": 1000000}', 'flag');

CREATE FUNCTION enforce_wallet_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.kind = 'wallet' AND NEW.balance_paise < 0 THEN RAISE EXCEPTION 'wallet balance cannot be negative'; END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER accounts_wallet_nonnegative BEFORE INSERT OR UPDATE OF balance_paise, kind ON accounts
FOR EACH ROW EXECUTE FUNCTION enforce_wallet_balance();
