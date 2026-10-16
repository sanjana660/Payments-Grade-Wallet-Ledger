"""Seed command placeholder: run inside the API image after schema creation."""
from app.db import transaction

with transaction() as conn:
        conn.execute("INSERT INTO users(name,email,phone,kyc_status) SELECT 'User '||g, 'user'||g||'@example.test', '+910000'||lpad(g::text,6,'0'), 'verified' FROM generate_series(1,1000) g")
        conn.execute("""WITH new_accounts AS (
            INSERT INTO accounts(kind) SELECT 'wallet' FROM generate_series(1,2000) RETURNING id
        ), numbered AS (
            SELECT id, row_number() OVER (ORDER BY id) AS n FROM new_accounts
        )
        INSERT INTO wallets(user_id,account_id,name)
        SELECT ((n - 1) % 1000) + 1, id, CASE WHEN n <= 1000 THEN 'Main' ELSE 'Savings' END
        FROM numbered""")
        conn.execute("""WITH generated AS (
            INSERT INTO transfers(type,status,amount_paise,source_account_id,destination_account_id,completed_at)
            SELECT 'peer','completed',100,4 + ((g - 1) % 2000),4 + (g % 2000),now()
            FROM generate_series(1,100000) g
            RETURNING id,amount_paise,source_account_id,destination_account_id
        )
        INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise)
        SELECT id,source_account_id,'debit',amount_paise FROM generated
        UNION ALL
        SELECT id,destination_account_id,'credit',amount_paise FROM generated""")
        conn.execute("""UPDATE accounts a SET balance_paise = COALESCE(x.delta, 0)
            FROM (SELECT account_id, sum(CASE WHEN direction='credit' THEN amount_paise ELSE -amount_paise END) AS delta
                        FROM ledger_entries GROUP BY account_id) x
            WHERE a.id=x.account_id AND a.kind='wallet'""")
