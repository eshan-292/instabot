# app.py
import os, threading, time
from datetime import datetime
from flask import Flask, jsonify
from dotenv import load_dotenv

# import your existing function
from post_reels import process_due_items, load_env

app = Flask(__name__)
load_dotenv()   # will be populated from Render "Environment" settings
load_env()

WINDOW_MIN = int(os.getenv("WINDOW_MIN", "20"))
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "60"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

running = True

def worker_loop():
    while running:
        try:
            process_due_items(WINDOW_MIN, dry_run=DRY_RUN)
        except Exception as e:
            print(f"[!] Loop error: {e}", flush=True)
        time.sleep(INTERVAL_SEC)

@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().isoformat()+"Z")

if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
