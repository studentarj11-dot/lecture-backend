"""
Daily Lecture MP3 — Backend
Flask API that monitors a YouTube channel RSS feed,
downloads new uploads as MP3, and serves them for Flutter app.
"""

import os
import re
import json
import logging
from datetime import datetime, timezone

import feedparser
import yt_dlp
from flask import Flask, jsonify, send_from_directory, abort
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# ── Load environment variables ────────────────────────────────────────────────
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
CHANNEL_ID          = os.getenv("YOUTUBE_CHANNEL_ID", "UCikpvN4t2F6RaaSZToD8W7Q")
BASE_URL            = os.getenv("BASE_URL", "http://localhost:5000")
RSS_URL             = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
STATE_FILE          = "state.json"
MIN_DURATION_SEC    = 180   # Skip videos shorter than 3 min (Shorts filter)
POLL_INTERVAL_MIN   = 30    # Check RSS every 30 minutes

# Audio files stored in static/audio/
AUDIO_DIR = os.path.join(os.path.dirname(__file__), "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)   # Allow Flutter app to call this API


# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    """Load persisted state from JSON file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_video_id": None, "latest": None}


def save_state(state: dict) -> None:
    """Persist state to JSON file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Video helpers ─────────────────────────────────────────────────────────────
def get_video_duration(video_id: str) -> int:
    """Return video duration in seconds using yt-dlp (no download)."""
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            return int(info.get("duration") or 0)
    except Exception as exc:
        logger.error("Duration check failed for %s: %s", video_id, exc)
        return 0


def process_video(video_id: str) -> str | None:
    """
    Download audio track and convert to MP3.
    Returns the filename on success, None on failure.
    """
    filename = f"{video_id}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)

    # Already processed — skip re-download
    if os.path.exists(filepath):
        logger.info("MP3 already exists: %s", filename)
        return filename

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(AUDIO_DIR, f"{video_id}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        if os.path.exists(filepath):
            logger.info("MP3 saved: %s", filename)
            return filename
        else:
            logger.error("MP3 file not found after download: %s", filepath)
            return None
    except Exception as exc:
        logger.error("Download failed for %s: %s", video_id, exc)
        return None


def extract_video_id(entry) -> str | None:
    """Extract YouTube video ID from an RSS feed entry."""
    # feedparser exposes yt:videoId as entry.yt_videoid
    vid = getattr(entry, "yt_videoid", None)
    if vid:
        return vid
    # Fallback: parse from link URL
    link = entry.get("link", "")
    match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", link)
    return match.group(1) if match else None


# ── Core RSS polling job ──────────────────────────────────────────────────────
def check_rss() -> None:
    """
    Poll the YouTube RSS feed.
    If a new video longer than MIN_DURATION_SEC is found, download its audio.
    """
    logger.info("Polling RSS feed: %s", RSS_URL)
    state = load_state()

    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as exc:
        logger.error("feedparser error: %s", exc)
        return

    if not feed.entries:
        logger.warning("RSS feed returned no entries")
        return

    # Entries are newest-first
    latest_entry = feed.entries[0]
    video_id = extract_video_id(latest_entry)

    if not video_id:
        logger.warning("Could not extract video ID from latest entry")
        return

    if video_id == state.get("last_video_id"):
        logger.info("No new video (latest is still %s)", video_id)
        return

    title     = latest_entry.get("title", "Unknown Lecture")
    published = latest_entry.get("published", datetime.now(timezone.utc).isoformat())

    logger.info("New video detected: '%s' (%s)", title, video_id)

    # ── Shorts filter ──────────────────────────────────────────────────────
    duration = get_video_duration(video_id)
    if duration < MIN_DURATION_SEC:
        logger.info(
            "Skipping short video (%ds < %ds): %s", duration, MIN_DURATION_SEC, title
        )
        # Still mark as seen so we don't keep re-checking it
        state["last_video_id"] = video_id
        save_state(state)
        return

    # ── Download MP3 ───────────────────────────────────────────────────────
    filename = process_video(video_id)
    if filename:
        state["last_video_id"] = video_id
        state["latest"] = {
            "video_id":    video_id,
            "title":       title,
            "published_at": published,
            "duration_sec": duration,
            "mp3_filename": filename,
            "mp3_url":     f"{BASE_URL}/download/{filename}",
        }
        save_state(state)
        logger.info("State updated successfully for: %s", title)


# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/latest", methods=["GET"])
def get_latest():
    """Return metadata for the latest processed lecture."""
    state = load_state()
    latest = state.get("latest")
    if not latest:
        return jsonify({"error": "No lecture available yet. Check back later."}), 404
    return jsonify(latest)


@app.route("/download/<path:filename>", methods=["GET"])
def download_file(filename: str):
    """Stream the MP3 file for download. Only .mp3 files allowed."""
    if not filename.endswith(".mp3"):
        abort(400, description="Only .mp3 files can be downloaded.")
    # Prevent directory traversal
    filename = os.path.basename(filename)
    if not os.path.exists(os.path.join(AUDIO_DIR, filename)):
        abort(404, description="MP3 not found.")
    return send_from_directory(AUDIO_DIR, filename, as_attachment=True)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint (useful for Render keep-alive pings)."""
    return jsonify({"status": "ok", "channel_id": CHANNEL_ID})


@app.route("/refresh", methods=["POST"])
def manual_refresh():
    """Manually trigger an RSS check (useful for testing)."""
    check_rss()
    state = load_state()
    return jsonify({"message": "RSS check triggered", "latest": state.get("latest")})


# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Run one check immediately on startup
    logger.info("Running initial RSS check on startup...")
    check_rss()

    # Schedule periodic background checks
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(check_rss, "interval", minutes=POLL_INTERVAL_MIN)
    scheduler.start()
    logger.info("Scheduler started — checking every %d minutes", POLL_INTERVAL_MIN)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
