# server_runner.py
import os, threading
from fastapi import FastAPI, Response, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel
import uvicorn
import post_reels as poster

app = FastAPI()
poster.load_env()

# optional auth for /run calls (set JOB_TOKEN in Render env)
JOB_TOKEN = os.getenv("JOB_TOKEN")

run_lock = threading.Lock()
last_status = {
    "started_at": None,
    "finished_at": None,
    "ran": False,
    "changed": False,
    "error": None,
}

class RunRequest(BaseModel):
    window_min: int = 20
    dry_run: bool = False
    also_story: bool = True
    max_items: int | None = None  # optional—see step 2

@app.get("/health")
def health():
    return {"status": "ok"}

def _do_run(req: RunRequest):
    import datetime as dt
    from datetime import timezone

    last_status.update({
        "started_at": dt.datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "ran": True,
        "changed": False,
        "error": None,
    })
    try:
        # If you add a max-items cap (step 2), pass it here.
        changed = poster.process_due_items(
            req.window_min, dry_run=req.dry_run, also_story=req.also_story
        )
        last_status.update({
            "changed": bool(changed),
            "finished_at": dt.datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        last_status.update({
            "error": str(e),
            "finished_at": dt.datetime.now(timezone.utc).isoformat(),
        })

@app.post("/run")
def run(req: RunRequest, background_tasks: BackgroundTasks, authorization: str | None = Header(None)):
    if JOB_TOKEN:
        if not authorization or not authorization.startswith("Bearer ") or authorization.split(" ",1)[1] != JOB_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # prevent overlapping runs
    if run_lock.locked():
        return {"status": "busy", "detail": "Another run is in progress"}

    def job():
        with run_lock:
            _do_run(req)

    background_tasks.add_task(job)
    # Return immediately so Render doesn't time out
    return {"status": "accepted"}

@app.get("/last")
def last():
    return last_status

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
