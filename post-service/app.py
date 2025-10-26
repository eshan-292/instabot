#!/usr/bin/env python3
import os
import threading
from fastapi import FastAPI, Response
from pydantic import BaseModel
import uvicorn

# Import your existing code
import post_reels as poster

app = FastAPI()

# Ensure .env is read
poster.load_env()

# Simple in-process lock to avoid overlapping runs
run_lock = threading.Lock()
last_status = {"ran": False, "error": None}

class RunRequest(BaseModel):
    window_min: int = 20
    dry_run: bool = False
    also_story: bool = True

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/run")
def run(req: RunRequest):
    if not run_lock.acquire(blocking=False):
        # Another run is still executing; skip to prevent overlap
        return {"status": "busy", "detail": "Another run in progress"}
    try:
        changed = poster.process_due_items(
            window_min=req.window_min,
            dry_run=req.dry_run,
            also_story=req.also_story,
        )
        last_status.update({"ran": True, "error": None})
        return {"status": "ok", "changed": bool(changed)}
    except Exception as e:
        last_status.update({"ran": True, "error": str(e)})
        return Response(content=str(e), status_code=500)
    finally:
        run_lock.release()

@app.get("/last")
def last():
    return last_status

if __name__ == "__main__":
    # Render expects the service to listen on $PORT
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
