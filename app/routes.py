import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from .db import transaction, connection
from .models import UserCreate, WalletCreate, TransferCreate, TopupCreate, WithdrawalCreate, DecisionCreate, AskRequest

router = APIRouter()

def error(code, message, status):
    raise HTTPException(status_code=status, detail={"code": code, "message": message, "request_id": "request"})

def require_key(value):
    if not value:
        error("MISSING_IDEMPOTENCY_KEY", "Idempotency-Key is required", 400)
    try: return UUID(value)
    except ValueError: error("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID", 400)

def body_hash(body): return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

def idempotent(conn, user_id, key, payload):
    digest = body_hash(payload)
    row = conn.execute("""INSERT INTO idempotency_keys(user_id,key,request_hash)
      VALUES (%s,%s,%s) ON CONFLICT (user_id,key) DO NOTHING RETURNING id""", (user_id,key,digest)).fetchone()
    if row: return digest, None
    old = conn.execute("SELECT request_hash,status,response_code,response_body FROM idempotency_keys WHERE user_id=%s AND key=%s FOR UPDATE", (user_id,key)).fetchone()
    if old["request_hash"] != digest: error("IDEMPOTENCY_BODY_MISMATCH", "The key was used with a different body", 422)
    if old["status"] == "running": error("REQUEST_IN_PROGRESS", "Retry shortly", 409)
    return digest, {"status_code": old["response_code"], "body": old["response_body"]}

def lock_accounts(conn, ids):
    return conn.execute("SELECT id,kind,balance_paise FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (sorted(ids),)).fetchall()

def post_transfer(user_id, key, payload, transfer_type, source_wallet, destination_wallet, amount, note=None):
    with transaction("READ COMMITTED") as conn:
        _, replay = idempotent(conn, user_id, key, payload)
        if replay:
            return JSONResponse(status_code=replay["status_code"], content=replay["body"])
        accounts = lock_accounts(conn, [source_wallet, destination_wallet])
        by_id = {row["id"]: row for row in accounts}
        if by_id[source_wallet]["kind"] == "wallet" and by_id[source_wallet]["balance_paise"] < amount:
            error("INSUFFICIENT_FUNDS", "Wallet balance is too low", 422)
        fraud_eligible = by_id[source_wallet]["kind"] == "wallet" and transfer_type == "peer"
        velocity = fraud_eligible and conn.execute("""SELECT count(*) AS count FROM transfers
          WHERE source_account_id=%s AND status='completed'
            AND created_at > now()-interval '10 minutes'""", (source_wallet,)).fetchone()["count"] >= 5
        average = conn.execute("""SELECT COALESCE(avg(amount_paise), 0) AS value FROM transfers
          WHERE source_account_id=%s AND status='completed'
            AND created_at > now()-interval '30 days'""", (source_wallet,)).fetchone()["value"] if fraud_eligible else 0
        spike = bool(fraud_eligible and average and amount > average * 5)
        status = "held" if velocity or spike else "completed"
        transfer = conn.execute("""INSERT INTO transfers(type,status,amount_paise,source_account_id,destination_account_id,note,completed_at)
          VALUES (%s,%s,%s,%s,%s,%s,CASE WHEN %s='completed' THEN now() END) RETURNING id""", (transfer_type,status,amount,source_wallet,destination_wallet,note,status)).fetchone()
        transfer_id = transfer["id"]
        if status == "completed":
            conn.execute("INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES (%s,%s,'debit',%s),(%s,%s,'credit',%s)", (transfer_id,source_wallet,amount,transfer_id,destination_wallet,amount))
            conn.execute("UPDATE accounts SET balance_paise=balance_paise-%s WHERE id=%s", (amount,source_wallet))
            conn.execute("UPDATE accounts SET balance_paise=balance_paise+%s WHERE id=%s", (amount,destination_wallet))
        else:
            rule = "velocity" if velocity else "amount_spike"
            conn.execute("INSERT INTO risk_flags(transfer_id,rule_id) SELECT %s,id FROM risk_rules WHERE name=%s", (transfer_id,rule))
        new_payee = conn.execute("SELECT count(*) = 0 AS first_transfer FROM transfers WHERE type='peer' AND source_account_id=%s AND destination_account_id=%s AND id<>%s AND status='completed'", (source_wallet,destination_wallet,transfer_id)).fetchone()["first_transfer"]
        if transfer_type == "peer" and new_payee and amount > 1000000:
            conn.execute("INSERT INTO risk_flags(transfer_id,rule_id) SELECT %s,id FROM risk_rules WHERE name='new_payee_large'", (transfer_id,))
        result = {"transfer_id": transfer_id, "status": status, "amount_paise": amount}
        conn.execute("UPDATE idempotency_keys SET status='completed',response_code=%s,response_body=%s WHERE user_id=%s AND key=%s", (201 if status == "completed" else 202,json.dumps(result),user_id,key))
        conn.execute("INSERT INTO transfer_status_audit(transfer_id,new_status,changed_by) VALUES (%s,%s,%s)", (transfer_id,status,user_id))
        return JSONResponse(status_code=201 if status == "completed" else 202, content=result)

@router.post("/users", status_code=201)
def create_user(body: UserCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    key = require_key(idempotency_key)
    with transaction() as conn:
        payload = body.model_dump()
        digest = body_hash(payload)
        existing = conn.execute("SELECT request_hash,status,response_code,response_body FROM idempotency_keys WHERE user_id IS NULL AND key=%s FOR UPDATE", (key,)).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                error("IDEMPOTENCY_BODY_MISMATCH", "The key was used with a different body", 422)
            if existing["status"] == "running":
                error("REQUEST_IN_PROGRESS", "Retry shortly", 409)
            return JSONResponse(status_code=existing["response_code"], content=existing["response_body"])
        conn.execute("INSERT INTO idempotency_keys(user_id,key,request_hash) VALUES (NULL,%s,%s)", (key,digest))
        user = conn.execute("INSERT INTO users(name,email,phone) VALUES (%s,%s,%s) RETURNING *", (body.name,body.email,body.phone)).fetchone()
        response = dict(user)
        conn.execute("UPDATE idempotency_keys SET status='completed',response_code=201,response_body=%s WHERE user_id IS NULL AND key=%s", (json.dumps(response, default=str),key))
        return response

@router.post("/users/{user_id}/wallets", status_code=201)
def create_wallet(user_id: int, body: WalletCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    key = require_key(idempotency_key)
    with transaction() as conn:
        payload = body.model_dump()
        _, replay = idempotent(conn,user_id,key,payload)
        if replay: return JSONResponse(status_code=replay["status_code"], content=replay["body"])
        account = conn.execute("INSERT INTO accounts(kind) VALUES ('wallet') RETURNING id").fetchone()
        wallet = conn.execute("INSERT INTO wallets(user_id,account_id,name) VALUES (%s,%s,%s) RETURNING *", (user_id,account["id"],body.name)).fetchone()
        response = dict(wallet)
        conn.execute("UPDATE idempotency_keys SET status='completed',response_code=201,response_body=%s WHERE user_id=%s AND key=%s", (json.dumps(response, default=str),user_id,key))
        return response

@router.get("/wallets/{wallet_id}/balance")
def balance(wallet_id: int):
    with connection() as conn:
        row = conn.execute("SELECT w.id,w.user_id,a.currency,a.balance_paise FROM wallets w JOIN accounts a ON a.id=w.account_id WHERE w.id=%s", (wallet_id,)).fetchone()
        if not row: error("NOT_FOUND","Wallet not found",404)
        return row

@router.get("/wallets/{wallet_id}/statement")
def statement(wallet_id: int, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    with connection() as conn:
        return conn.execute("""SELECT e.id,e.transfer_id,e.direction,e.amount_paise,e.created_at,t.type,t.status
          FROM ledger_entries e JOIN wallets w ON w.account_id=e.account_id JOIN transfers t ON t.id=e.transfer_id
          WHERE w.id=%s ORDER BY e.created_at DESC,e.id DESC LIMIT %s OFFSET %s""", (wallet_id,limit,offset)).fetchall()

@router.post("/transfers", status_code=201)
def transfer(body: TransferCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    key = require_key(idempotency_key)
    with connection() as conn:
        owner = conn.execute("SELECT user_id,account_id FROM wallets WHERE id=%s", (body.from_wallet_id,)).fetchone()
        dest = conn.execute("SELECT account_id FROM wallets WHERE id=%s", (body.to_wallet_id,)).fetchone()
    if not owner or not dest: error("NOT_FOUND","Wallet not found",404)
    return post_transfer(owner["user_id"],key,body.model_dump(),"peer",owner["account_id"],dest["account_id"],body.amount_paise,body.note)

@router.post("/transfers/topup", status_code=201)
def topup(body: TopupCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    key = require_key(idempotency_key)
    with connection() as conn:
        row = conn.execute("SELECT user_id,account_id FROM wallets WHERE id=%s", (body.wallet_id,)).fetchone()
        system = conn.execute("SELECT account_id FROM system_accounts WHERE name='cash_in'").fetchone()
    if not row: error("NOT_FOUND","Wallet not found",404)
    return post_transfer(row["user_id"],key,body.model_dump(),"topup",system["account_id"],row["account_id"],body.amount_paise)

@router.post("/transfers/withdraw", status_code=201)
def withdraw(body: WithdrawalCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    key = require_key(idempotency_key)
    with connection() as conn:
        row = conn.execute("SELECT user_id,account_id FROM wallets WHERE id=%s", (body.wallet_id,)).fetchone()
        system = conn.execute("SELECT account_id FROM system_accounts WHERE name='cash_out'").fetchone()
    if not row: error("NOT_FOUND","Wallet not found",404)
    return post_transfer(row["user_id"],key,body.model_dump(),"withdrawal",row["account_id"],system["account_id"],body.amount_paise)

@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int):
    with connection() as conn:
        transfer = conn.execute("SELECT * FROM transfers WHERE id=%s", (transfer_id,)).fetchone()
        if not transfer: error("NOT_FOUND","Transfer not found",404)
        transfer["entries"] = conn.execute("SELECT * FROM ledger_entries WHERE transfer_id=%s ORDER BY id", (transfer_id,)).fetchall()
        transfer["history"] = conn.execute("SELECT * FROM transfer_status_audit WHERE transfer_id=%s ORDER BY id", (transfer_id,)).fetchall()
        return transfer

@router.post("/transfers/{transfer_id}/reverse", status_code=201)
def reverse_transfer(transfer_id: int, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        key = require_key(idempotency_key)
        with transaction("READ COMMITTED") as conn:
                original = conn.execute("SELECT * FROM transfers WHERE id=%s FOR UPDATE", (transfer_id,)).fetchone()
                if not original: error("NOT_FOUND", "Transfer not found", 404)
                if original["status"] != "completed": error("INVALID_STATUS", "Only completed transfers can be reversed", 409)
                owner = conn.execute("""SELECT w.user_id FROM ledger_entries e JOIN wallets w ON w.account_id=e.account_id
                    WHERE e.transfer_id=%s ORDER BY e.id LIMIT 1""", (transfer_id,)).fetchone()
                if not owner: error("INVALID_TRANSFER", "Transfer has no wallet entry", 422)
                payload = {"transfer_id": transfer_id}
                _, replay = idempotent(conn, owner["user_id"], key, payload)
                if replay: return JSONResponse(status_code=replay["status_code"], content=replay["body"])
                entries = conn.execute("SELECT account_id,direction,amount_paise FROM ledger_entries WHERE transfer_id=%s ORDER BY id", (transfer_id,)).fetchall()
                lock_accounts(conn, [entry["account_id"] for entry in entries])
                reversal = conn.execute("INSERT INTO transfers(type,status,amount_paise,original_transfer_id,completed_at) VALUES ('reversal','completed',%s,%s,now()) RETURNING id", (original["amount_paise"], transfer_id)).fetchone()
                for entry in entries:
                        opposite = "credit" if entry["direction"] == "debit" else "debit"
                        conn.execute("INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES (%s,%s,%s,%s)", (reversal["id"], entry["account_id"], opposite, entry["amount_paise"]))
                        delta = entry["amount_paise"] if opposite == "credit" else -entry["amount_paise"]
                        conn.execute("UPDATE accounts SET balance_paise=balance_paise+%s WHERE id=%s", (delta, entry["account_id"]))
                result = {"transfer_id": reversal["id"], "status": "completed", "original_transfer_id": transfer_id}
                conn.execute("UPDATE idempotency_keys SET status='completed',response_code=201,response_body=%s WHERE user_id=%s AND key=%s", (json.dumps(result), owner["user_id"], key))
                conn.execute("INSERT INTO transfer_status_audit(transfer_id,new_status,changed_by) VALUES (%s,'completed',%s)", (reversal["id"], owner["user_id"]))
                return result

@router.post("/admin/reconciliation/run", status_code=202)
def reconciliation_run():
    with transaction("REPEATABLE READ") as conn:
        try: conn.execute("SELECT job_name FROM job_locks WHERE job_name='reconciliation' FOR UPDATE NOWAIT")
        except Exception: error("RECONCILIATION_RUNNING","Another run is active",409)
        run = conn.execute("INSERT INTO reconciliation_runs(status) VALUES ('running') RETURNING id").fetchone()
        run_id = run["id"]
        checks = [
          ("global_balance", "SELECT COALESCE(sum(amount_paise) FILTER (WHERE direction='debit'),0)=COALESCE(sum(amount_paise) FILTER (WHERE direction='credit'),0) AS ok FROM ledger_entries", None),
          ("transfer_balance", "SELECT transfer_id FROM ledger_entries GROUP BY transfer_id HAVING sum(amount_paise) FILTER (WHERE direction='debit') IS DISTINCT FROM sum(amount_paise) FILTER (WHERE direction='credit')", None),
          ("stored_balance", "SELECT a.id FROM accounts a LEFT JOIN ledger_entries e ON e.account_id=a.id GROUP BY a.id,a.balance_paise HAVING a.balance_paise IS DISTINCT FROM COALESCE(sum(CASE WHEN e.direction='credit' THEN e.amount_paise ELSE -e.amount_paise END),0)", None),
          ("no_overdraft", "SELECT a.id FROM accounts a JOIN wallets w ON w.account_id=a.id WHERE a.balance_paise < 0", None),
          ("orphan_transfer", "SELECT t.id FROM transfers t LEFT JOIN ledger_entries e ON e.transfer_id=t.id WHERE t.status <> 'held' GROUP BY t.id HAVING count(e.id)=0", None),
        ]
        issues = 0
        for name, sql, _ in checks:
            rows = conn.execute(sql).fetchall()
            if name == "global_balance": rows = [] if rows[0]["ok"] else [{"id": None}]
            for row in rows:
                conn.execute("INSERT INTO reconciliation_issues(run_id,check_name,subject_id,expected,actual) VALUES (%s,%s,%s,%s,%s)", (run_id,name,row.get("id") or row.get("transfer_id"),"balanced","mismatch")); issues += 1
        conn.execute("UPDATE reconciliation_runs SET ended_at=now(),status='completed',issue_count=%s WHERE id=%s", (issues,run_id))
        return {"run_id": run_id, "status": "completed", "issue_count": issues}

@router.get("/admin/reconciliation/runs/{run_id}")
def reconciliation_report(run_id: int):
    with connection() as conn:
        run = conn.execute("SELECT * FROM reconciliation_runs WHERE id=%s", (run_id,)).fetchone()
        if not run: error("NOT_FOUND","Run not found",404)
        run["issues"] = conn.execute("SELECT * FROM reconciliation_issues WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
        return run

@router.get("/admin/risk/queue")
def risk_queue():
    with connection() as conn:
        return conn.execute("SELECT * FROM transfers WHERE status='held' OR id IN (SELECT transfer_id FROM risk_flags) ORDER BY created_at").fetchall()

@router.post("/admin/risk/{transfer_id}/decision")
def risk_decision(transfer_id: int, body: DecisionCreate):
    if body.decision not in ("release", "reverse"):
        error("INVALID_DECISION", "Use release or reverse", 422)
    with transaction() as conn:
        transfer = conn.execute("SELECT * FROM transfers WHERE id=%s FOR UPDATE", (transfer_id,)).fetchone()
        if not transfer:
            error("NOT_FOUND", "Transfer not found", 404)
        if transfer["status"] != "held":
            error("INVALID_STATUS", "Transfer is not held", 409)
        if body.decision == "release":
            lock_accounts(conn, [transfer["source_account_id"], transfer["destination_account_id"]])
            if transfer["source_account_id"]:
                source = conn.execute("SELECT kind,balance_paise FROM accounts WHERE id=%s", (transfer["source_account_id"],)).fetchone()
                if source["kind"] == "wallet" and source["balance_paise"] < transfer["amount_paise"]:
                    error("INSUFFICIENT_FUNDS", "Wallet balance is too low", 422)
            conn.execute("INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES (%s,%s,'debit',%s),(%s,%s,'credit',%s)", (transfer_id,transfer["source_account_id"],transfer["amount_paise"],transfer_id,transfer["destination_account_id"],transfer["amount_paise"]))
            conn.execute("UPDATE accounts SET balance_paise=balance_paise-%s WHERE id=%s", (transfer["amount_paise"],transfer["source_account_id"]))
            conn.execute("UPDATE accounts SET balance_paise=balance_paise+%s WHERE id=%s", (transfer["amount_paise"],transfer["destination_account_id"]))
            conn.execute("UPDATE transfers SET status='completed',completed_at=now() WHERE id=%s", (transfer_id,))
            conn.execute("INSERT INTO transfer_status_audit(transfer_id,old_status,new_status) VALUES (%s,'held','completed')", (transfer_id,))
            return {"transfer_id": transfer_id, "status": "completed"}
        conn.execute("UPDATE transfers SET status='reversed',completed_at=now() WHERE id=%s", (transfer_id,))
        conn.execute("INSERT INTO transfer_status_audit(transfer_id,old_status,new_status) VALUES (%s,'held','reversed')", (transfer_id,))
        return {"transfer_id": transfer_id, "status": "reversed"}

@router.post("/users/{user_id}/statement/ask")
def ask_statement(user_id: int, body: AskRequest):
    question = body.question.lower()
    dates = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})", question)
    person_match = re.search(r"(?:to|from)\s+([a-z][a-z .'-]*)", question)
    if not dates or not person_match:
        return {"message": "I can only answer supported statement questions.", "transfer_ids": []}
    start, end = dates.groups()
    person = person_match.group(1).strip()
    if "largest" in question:
        operation = "largest"
    elif "how many" in question or "number of" in question or "count" in question:
        operation = "count"
    elif "received" in question:
        operation = "received"
    elif "sent" in question:
        operation = "sent"
    else:
        return {"message": "I can only answer supported statement questions.", "transfer_ids": []}
    with connection() as conn:
        if operation == "sent":
            relation = "t.source_account_id IN (SELECT account_id FROM wallets WHERE user_id=%s) AND t.destination_account_id IN (SELECT w.account_id FROM wallets w JOIN users u ON u.id=w.user_id WHERE lower(u.name) LIKE %s)"
        else:
            relation = "t.destination_account_id IN (SELECT account_id FROM wallets WHERE user_id=%s) AND t.source_account_id IN (SELECT w.account_id FROM wallets w JOIN users u ON u.id=w.user_id WHERE lower(u.name) LIKE %s)"
        rows = conn.execute(f"""SELECT t.id,t.amount_paise FROM transfers t
          WHERE t.status='completed' AND t.created_at >= %s AND t.created_at < %s
            AND {relation} ORDER BY t.amount_paise DESC,t.id""", (start,end,user_id,"%" + person + "%")).fetchall()
        ids = [row["id"] for row in rows]
        if operation == "count":
            return {"count": len(rows), "transfer_ids": ids}
        if operation == "largest":
            return {"amount_paise": rows[0]["amount_paise"] if rows else 0, "transfer_ids": ids[:1]}
        return {"amount_paise": sum(row["amount_paise"] for row in rows), "transfer_ids": ids}
