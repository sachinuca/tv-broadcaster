import os
import re
import json
import time
import urllib.request
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="TV Broadcaster Backend")

# Ensure static folder exists
os.makedirs("static", exist_ok=True)

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "pushed_link": "",
    "channel_1": "",  # Priority 1 YouTube Channel ID or Handle
    "channel_2": "",  # Priority 2 YouTube Channel ID or Handle
    "channel_3": "",  # Priority 3 (Lowest Fallback) Direct Stream URL or YouTube Channel
}

# In-memory runtime state
app_state = {
    "last_heartbeat": 0.0,
    "current_playing_url": "",
    "current_playing_type": "none"
}

# Simple cache for YouTube Live checks to prevent throttling/delays
# Key: channel_id_or_handle, Value: { "live_url": str or None, "expiry": float }
live_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_SECS = 60.0

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

# Create config file if not exists
if not os.path.exists(CONFIG_FILE):
    save_config(DEFAULT_CONFIG)

class ConfigModel(BaseModel):
    pushed_link: str
    channel_1: str
    channel_2: str
    channel_3: str

def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def extract_youtube_video_id(url: str) -> Optional[str]:
    # Match various youtube URL formats to extract 11-char video ID
    patterns = [
        r"(?:v=|\/v\/|embed\/|youtu.be\/|\/shorts\/|^)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def fetch_youtube_live_url_raw(channel_identifier: str) -> Optional[str]:
    """Helper to check if a YouTube channel is currently live and return its video URL."""
    if not channel_identifier:
        return None
        
    # Format channel identifier to live URL
    channel_identifier = channel_identifier.strip()
    if channel_identifier.startswith("UC"):
        url = f"https://www.youtube.com/channel/{channel_identifier}/live"
    elif channel_identifier.startswith("@"):
        url = f"https://www.youtube.com/{channel_identifier}/live"
    else:
        # Standardize handle
        if "youtube.com" in channel_identifier:
            url = channel_identifier
            if not url.endswith("/live"):
                url = url.rstrip("/") + "/live"
        else:
            url = f"https://www.youtube.com/@{channel_identifier}/live"
            
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            final_url = response.geturl()
            # If redirected directly to a watch URL, the channel is live!
            if "watch?v=" in final_url:
                return final_url
                
            # Otherwise, read page content to look for live indicator JSON
            content = response.read().decode('utf-8', errors='ignore')
            if 'isLive":true' in content or '{"iconType":"LIVE"}' in content or '"style":"LIVE"' in content:
                video_ids = re.findall(r'"videoId":"([^"]+)"', content)
                if video_ids:
                    return f"https://www.youtube.com/watch?v={video_ids[0]}"
    except Exception as e:
        print(f"Error checking YouTube Live for {channel_identifier}: {e}")
    return None

def get_channel_live_url(channel_identifier: str) -> Optional[str]:
    """Retrieves live URL with cache support to prevent heavy load on YouTube."""
    if not channel_identifier:
        return None
        
    now = time.time()
    if channel_identifier in live_cache:
        cached = live_cache[channel_identifier]
        if now < cached["expiry"]:
            return cached["live_url"]
            
    # Cache miss or expired
    live_url = fetch_youtube_live_url_raw(channel_identifier)
    live_cache[channel_identifier] = {
        "live_url": live_url,
        "expiry": now + CACHE_DURATION_SECS
    }
    return live_url

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/config")
def save_config_api(config: ConfigModel):
    save_config(config.model_dump())
    return {"status": "success", "config": load_config()}

@app.post("/api/clear-push")
def clear_push():
    config = load_config()
    config["pushed_link"] = ""
    save_config(config)
    return {"status": "success", "config": config}

@app.get("/api/play-status")
def play_status():
    """Polled by the TV app. Heartbeat is updated and priority is evaluated."""
    app_state["last_heartbeat"] = time.time()
    config = load_config()
    
    play_url = ""
    play_type = "none"
    is_youtube = False
    
    # Priority 1: Pushed Link (Highest Priority)
    if config.get("pushed_link"):
        play_url = config["pushed_link"]
        play_type = "push"
        is_youtube = is_youtube_url(play_url)
    else:
        # Priority 2: Channel 1 (if Live)
        ch1 = config.get("channel_1")
        ch1_live = get_channel_live_url(ch1) if ch1 else None
        if ch1_live:
            play_url = ch1_live
            play_type = "channel1"
            is_youtube = True
        else:
            # Priority 3: Channel 2 (if Live)
            ch2 = config.get("channel_2")
            ch2_live = get_channel_live_url(ch2) if ch2 else None
            if ch2_live:
                play_url = ch2_live
                play_type = "channel2"
                is_youtube = True
            else:
                # Priority 4: Channel 3 / Fallback (Lowest Priority)
                ch3 = config.get("channel_3")
                if ch3:
                    # Check if it's a YouTube channel and if it's live
                    if not is_youtube_url(ch3) and (ch3.startswith("@") or ch3.startswith("UC") or not ch3.startswith("http")):
                        # It is a YouTube channel handle/ID, let's check live
                        ch3_live = get_channel_live_url(ch3)
                        if ch3_live:
                            play_url = ch3_live
                            play_type = "channel3"
                            is_youtube = True
                        else:
                            # If not live, fall back to channel's home URL
                            if ch3.startswith("UC"):
                                play_url = f"https://www.youtube.com/channel/{ch3}"
                            else:
                                play_url = f"https://www.youtube.com/{ch3 if ch3.startswith('@') else '@' + ch3}"
                            play_type = "channel3"
                            is_youtube = True
                    else:
                        # It's a direct URL (YouTube video or HLS stream)
                        play_url = ch3
                        play_type = "channel3"
                        is_youtube = is_youtube_url(ch3)
                else:
                    play_url = ""
                    play_type = "none"
                    is_youtube = False
                    
    app_state["current_playing_url"] = play_url
    app_state["current_playing_type"] = play_type
    
    return {
        "url": play_url,
        "type": play_type,
        "is_youtube": is_youtube,
        "youtube_id": extract_youtube_video_id(play_url) if is_youtube else None
    }

@app.get("/api/status")
def get_status():
    """Returns runtime state for the web dashboard."""
    now = time.time()
    last_hb = app_state["last_heartbeat"]
    is_online = (now - last_hb) < 90.0 if last_hb > 0 else False
    
    config = load_config()
    
    # Pre-fetch cached channel live status info to show on dashboard
    ch1_status = "LIVE" if (config.get("channel_1") and get_channel_live_url(config["channel_1"])) else "OFFLINE"
    ch2_status = "LIVE" if (config.get("channel_2") and get_channel_live_url(config["channel_2"])) else "OFFLINE"
    
    # Channel 3 status
    ch3 = config.get("channel_3")
    ch3_status = "OFFLINE"
    if ch3:
        if not is_youtube_url(ch3) and (ch3.startswith("@") or ch3.startswith("UC") or not ch3.startswith("http")):
            ch3_status = "LIVE" if get_channel_live_url(ch3) else "OFFLINE"
        else:
            ch3_status = "DIRECT"

    return {
        "is_online": is_online,
        "last_heartbeat": last_hb,
        "last_heartbeat_formatted": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_hb)) if last_hb > 0 else "Never",
        "current_playing_url": app_state["current_playing_url"],
        "current_playing_type": app_state["current_playing_type"],
        "channels_status": {
            "channel_1": ch1_status,
            "channel_2": ch2_status,
            "channel_3": ch3_status
        }
    }

# Mount static files folder
app.mount("/static", StaticFiles(directory="static"), name="static")
