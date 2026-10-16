import time
from .routes import reconciliation_run

if __name__ == "__main__":
    while True:
        try:
            reconciliation_run()
        except Exception as exc:
            print(f"reconciliation skipped: {exc}", flush=True)
        time.sleep(86400)
