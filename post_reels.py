#!/usr/bin/env python3
"""
post_reels.py — Publish due Instagram Reels according to reels/schedule.json.

Prereqs:
  - Instagram Business/Creator account linked to FB Page
  - IG Graph API access token with instagram_content_publish
  - IG user id (numeric)
  - Publicly accessible video URLs (schedule['public_video_url'] or via PUBLIC_BASE_URL)

Env (loaded from .env if present):
  IG_ACCESS_TOKEN   - Long-lived user access token (with instagram_content_publish)
  IG_USER_ID        - Instagram user id (numeric, not @handle)
  PUBLIC_BASE_URL   - Optional. If set, derive video URL as {PUBLIC_BASE_URL}/reels/<id>/reel.mp4

Files:
  Reads : reels/schedule.json
  Writes: reels/schedule.json (updates records after publish)

Usage:
  python post_reels.py --window-min 20          # one-shot, publish items due in last 20 min
  python post_reels.py --watch                  # keep running, check every minute
  python post_reels.py --dry-run --window-min 5 # simulate without publishing
"""

import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

# ---------- Paths ----------
REEL_DIR = Path("reels")
SCHEDULE_JSON = REEL_DIR / "schedule.json"

# ---------- Graph API ----------
GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}/"

# ---------- Utilities ----------
def load_env():
    # Load .env if present so os.getenv works
    load_dotenv()

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def load_schedule():
    if not SCHEDULE_JSON.exists():
        return []
    with open(SCHEDULE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_schedule(records):
    with open(SCHEDULE_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

def is_due(iso_str, window_minutes):
    """
    Consider due if scheduled time <= now (UTC) and not older than window_minutes.
    schedule["post_at_iso"] is expected to include tzinfo (Asia/Kolkata from your generator).
    """
    dt_local = datetime.fromisoformat(iso_str)
    dt_utc = dt_local.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    delta_min = (now_utc - dt_utc).total_seconds() / 60.0
    return delta_min >= 0 and delta_min < window_minutes

def resolve_video_url(rec, public_base_url):
    """
    Prefer explicit 'public_video_url'. Otherwise build from PUBLIC_BASE_URL like:
    {PUBLIC_BASE_URL.rstrip('/')}/reels/<id>/reel.mp4
    """
    if rec.get("public_video_url"):
        return rec["public_video_url"]
    if public_base_url:
        rel = f"reels/{rec['id']}/reel.mp4"
        return urljoin(public_base_url.rstrip("/") + "/", rel)
    raise RuntimeError(
        "No public_video_url. Set PUBLIC_BASE_URL or ensure schedule record has 'public_video_url'."
    )

def build_caption(rec):
    main = (rec.get("post_caption_main") or "").strip()
    hashtags = (rec.get("post_caption_hashtags") or "").strip()
    if hashtags:
        return f"{main}\n\n{hashtags}"
    return main

def http_request(method, url, *, params=None, data=None, json_body=None,
                 retries=5, backoff=2.0, ok=(200,)):
    headers = {
        "User-Agent": "IGReelsPoster/1.0 (+https://example.com)",
    }
    for attempt in range(retries):
        try:
            resp = requests.request(
                method, url, params=params, data=data, json=json_body,
                timeout=60, headers=headers
            )
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
            continue

        if resp.status_code in ok:
            # some endpoints return empty body on success — guard JSON parsing
            if resp.text.strip():
                return resp.json()
            return {}
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(backoff * (2 ** attempt))
            continue

        # Raise informative error
        try:
            detail = resp.json()
        except Exception:
            detail = {"text": resp.text[:300]}
        raise RuntimeError(f"HTTP {resp.status_code} for {url}: {detail}")

    raise RuntimeError(f"Failed after {retries} retries for {url}")

# ---------- IG Graph helpers ----------
# def create_reels_container(ig_user_id, access_token, video_url, caption,
#                            share_to_feed=True):
#     url = f"{GRAPH_BASE}{ig_user_id}/media"
#     payload = {
#         "media_type": "REELS",
#         "video_url": video_url,
#         "caption": caption,
#         "share_to_feed": "true" if share_to_feed else "false",
#         "access_token": access_token,
#     }
#     data = http_request("POST", url, data=payload)
#     # returns {"id": "<creation_id>"}
#     return data["id"]

def create_media_container(ig_user_id, access_token, *, media_type, video_url=None, image_url=None,
                           caption=None, share_to_feed=True):
    url = f"{GRAPH_BASE}{ig_user_id}/media"
    payload = {
        "media_type": media_type,  # "REELS" or "STORIES" (or "IMAGE")
        "access_token": access_token,
    }
    if video_url:
        payload["video_url"] = video_url
    if image_url:
        payload["image_url"] = image_url
    if caption is not None:
        payload["caption"] = caption

    # Only meaningful for REELS (ignored by STORIES)
    if media_type == "REELS":
        payload["share_to_feed"] = "true" if share_to_feed else "false"

    data = http_request("POST", url, data=payload)
    return data["id"]

def post_story_from_url(ig_user_id, access_token, media_url, is_video=True, caption=""):
    creation_id = create_media_container(
        ig_user_id=ig_user_id,
        access_token=access_token,
        media_type="STORIES",
        video_url=media_url if is_video else None,
        image_url=None if is_video else media_url,
        caption=caption,
    )
    wait_until_processed(creation_id, access_token)
    media_id = publish_media(ig_user_id, access_token, creation_id)
    return {"creation_id": creation_id, "media_id": media_id}



def wait_until_processed(creation_id, access_token, poll_sec=6, timeout_sec=600):
    """
    Poll the container's status until 'FINISHED' (else 'ERROR' or timeout).
    """
    url = f"{GRAPH_BASE}{creation_id}"
    params = {"fields": "status_code", "access_token": access_token}
    t0 = time.time()
    while True:
        data = http_request("GET", url, params=params)
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Processing failed for container {creation_id}.")
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"Processing timeout for container {creation_id} (last={status}).")
        time.sleep(poll_sec)

def publish_media(ig_user_id, access_token, creation_id):
    url = f"{GRAPH_BASE}{ig_user_id}/media_publish"
    data = http_request("POST", url, data={"creation_id": creation_id, "access_token": access_token})
    # returns {"id":"<ig_media_id>"}
    return data["id"]

# def post_one(rec, ig_user_id, access_token, public_base_url, *, dry_run=False):
#     video_url = resolve_video_url(rec, public_base_url)
#     caption = build_caption(rec)

#     if dry_run:
#         return {
#             "dry_run": True,
#             "video_url": video_url,
#             "caption_preview": caption[:160] + ("..." if len(caption) > 160 else ""),
#         }

#     creation_id = create_reels_container(
#         ig_user_id=ig_user_id,
#         access_token=access_token,
#         video_url=video_url,
#         caption=caption,
#         share_to_feed=True,
#     )
#     wait_until_processed(creation_id, access_token)
#     media_id = publish_media(ig_user_id, access_token, creation_id)
#     return {
#         "creation_id": creation_id,
#         "media_id": media_id,
#         "video_url": video_url,
#     }

def post_one(rec, ig_user_id, access_token, public_base_url, *, dry_run=False, also_story=False):
    video_url = resolve_video_url(rec, public_base_url)
    caption = build_caption(rec)

    if dry_run:
        out = {
            "dry_run": True,
            "video_url": video_url,
            "caption_preview": caption[:160] + ("..." if len(caption) > 160 else ""),
        }
        if also_story:
            out["also_story"] = True
        return out

    # REEL
    creation_id = create_media_container(
        ig_user_id=ig_user_id,
        access_token=access_token,
        media_type="REELS",
        video_url=video_url,
        caption=caption,
        share_to_feed=True,
    )
    wait_until_processed(creation_id, access_token)
    reel_media_id = publish_media(ig_user_id, access_token, creation_id)

    result = {
        "creation_id": creation_id,
        "media_id": reel_media_id,
        "video_url": video_url,
    }

    # STORY (same media) — optional
    if also_story:
        story = post_story_from_url(ig_user_id, access_token, media_url=video_url, is_video=True, caption=caption)
        result["story_media_id"] = story["media_id"]

    return result

# ---------- Main cycle ----------
def process_due_items(window_min, *, dry_run=False, also_story=False):
    access_token = os.getenv("IG_ACCESS_TOKEN")
    ig_user_id   = os.getenv("IG_USER_ID")
    public_base  = os.getenv("PUBLIC_BASE_URL")  # optional

    if not access_token or not ig_user_id:
        raise SystemExit("Missing IG_ACCESS_TOKEN or IG_USER_ID in environment (load via .env or export).")

    schedule = load_schedule()
    if not schedule:
        print("[i] No schedule.json found or empty. Nothing to post.")
        return False

    changed = False
    for rec in schedule:
        # already handled?
        if rec.get("published_at_iso"):
            continue

        post_at_iso = rec.get("post_at_iso")
        # print(f"[i] Checking reel {rec['id']} scheduled at {post_at_iso}")
        if not post_at_iso:
            # not scheduled — skip silently
            continue

        if not is_due(post_at_iso, window_min):
            continue

        reel_id = rec["id"]
        print(f"[→] Posting {reel_id} (scheduled {post_at_iso})")

        try:
            result = post_one(rec, ig_user_id, access_token, public_base, dry_run=dry_run, also_story=also_story)
        except Exception as e:
            rec["publish_error"] = str(e)
            rec["publish_attempted_at_iso"] = now_utc_iso()
            print(f"[x] Failed {reel_id}: {e}")
            changed = True
            continue

        # success / dry-run annotate
        rec["publish_attempted_at_iso"] = now_utc_iso()
        if dry_run:
            rec["dry_run_info"] = result
            print(f"[✓] Dry-run would publish {reel_id} → {result.get('video_url')}")
        else:
            rec["published_at_iso"] = now_utc_iso()
            rec["ig_creation_id"] = result["creation_id"]
            rec["ig_media_id"] = result["media_id"]
            rec.setdefault("public_video_url", result["video_url"])
            print(f"[✓] Published {reel_id} → media_id={result['media_id']}")

        changed = True

    if changed:
        save_schedule(schedule)
        print(f"[✓] schedule.json updated.")
    else:
        print("[i] Nothing due right now.")

    return changed

def main():
    load_env()

    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=int, default=20,
                    help="Post items scheduled within the last N minutes.")
    ap.add_argument("--watch", action="store_true",
                    help="Keep running, check every minute.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not call the IG API; simulate only.")
    ap.add_argument("--also-story", action="store_true",
                help="After posting the Reel, also post the same media as a Story.", default=True)

    args = ap.parse_args()

    if args.watch:
        print("[i] Watching for due items every 60s...")
        while True:
            try:
                process_due_items(args.window_min, dry_run=args.dry_run, also_story=args.also_story)
            except KeyboardInterrupt:
                print("\n[!] Stopped by user.")
                break
            except Exception as e:
                print(f"[!] Fatal error in loop: {e}")
            time.sleep(60)
    else:
        process_due_items(args.window_min, dry_run=args.dry_run, also_story=args.also_story)

if __name__ == "__main__":
    main()
