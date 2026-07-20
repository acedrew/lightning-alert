import os
import datetime
import asyncio
import math
import logging
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv
from contextlib import asynccontextmanager

# Load env variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("lightning-alert")

# Global State
state = {
    "active": False,
    "start_time": None,
    "end_time": None,
    "timeout_hours": None,
    "type": None,  # "circle" or "polygon"
    "coordinates": None,  # {center, radius} or {polygon}
    "checks_count": 0,
    "detections_count": 0,
    "interval_minutes": 5.0,
    "recent_strikes": [],  # List of dicts
    "logs": [],  # List of status logs
    "seen_strike_ids": set(),  # Deduplication set
    "last_error": None,  # Last API or system error message
}

# Active background task reference
tracking_task: Optional[asyncio.Task] = None

def add_log(message: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    state["logs"].append(log_entry)
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]
    logger.info(log_entry)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    global tracking_task
    if tracking_task:
        state["active"] = False
        tracking_task.cancel()
        try:
            await tracking_task
        except asyncio.CancelledError:
            pass

import json

DATA_DIR = "/app/data" if os.path.exists("/app/data") else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

def load_config() -> Dict[str, str]:
    # Force reloading .env to pick up any changes
    load_dotenv(override=True)
    
    config = {"xweather_api_key": "", "discord_webhook": ""}
    
    # 1. Read environment variables
    env_key = os.getenv("XWEATHER_API_KEY", "").strip()
    env_webhook = os.getenv("DISCORD_WEBHOOK", "").strip()
    
    if env_key:
        config["xweather_api_key"] = env_key
    if env_webhook:
        config["discord_webhook"] = env_webhook
        
    # 2. Override with config.json if non-empty fields exist
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
                file_key = data.get("xweather_api_key", "").strip()
                file_webhook = data.get("discord_webhook", "").strip()
                if file_key:
                    config["xweather_api_key"] = file_key
                if file_webhook:
                    config["discord_webhook"] = file_webhook
        except Exception as e:
            logger.error(f"Failed to read config.json: {e}")
            
    return config

app = FastAPI(title="Flash Finder", lifespan=lifespan)

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StartTrackingRequest(BaseModel):
    type: str  # "circle" or "polygon"
    coordinates: Dict[str, Any]
    timeout_hours: float  # 1 to 4 hours
    interval_minutes: float = 5.0  # 1 to 5 minutes

async def send_discord_message(payload: Dict[str, Any]):
    config = load_config()
    webhook_url = config.get("discord_webhook", "")
    if not webhook_url:
        logger.error("Discord webhook URL not found in config or env!")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10.0)
            if resp.status_code >= 400:
                logger.error(f"Discord webhook error {resp.status_code}: {resp.text}")
                return False
            return True
    except Exception as e:
        logger.error(f"Failed to send Discord alert: {e}")
        return False

# Haversine distance in meters
def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000  # meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# Calculate bearing in degrees from point 1 to point 2
def calculate_bearing(lat1, lon1, lat2, lon2):
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - \
        math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    
    theta = math.atan2(y, x)
    bearing = (math.degrees(theta) + 360) % 360
    return bearing

# Ray casting algorithm for polygon containment check
def is_point_in_polygon(lat, lon, polygon):
    x, y = lon, lat
    n = len(polygon)
    inside = False
    p1x, p1y = polygon[0][1], polygon[0][0]
    for i in range(n + 1):
        p2x, p2y = polygon[i % n][1], polygon[i % n][0]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xints = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xints:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside

async def trigger_discord_alert(strikes: List[Dict[str, Any]]):
    # Sort strikes by distance if available
    strikes_sorted = sorted(strikes, key=lambda s: s.get("distance_meters") or 0)
    count = len(strikes_sorted)
    
    # Danger orange color from visual system is #f6511d, which is 16143645 in decimal
    color_val = 16143645
    
    desc = f"🚨 **Lightning Alert!** {count} strike(s) detected within your monitored area."
    
    embed_fields = []
    
    if state["type"] == "circle":
        center = state["coordinates"]["center"]
        radius_km = state["coordinates"]["radius"] / 1000.0
        embed_fields.append({
            "name": "Monitored Region",
            "value": f"Circle centered at `{center[0]:.4f}, {center[1]:.4f}`\nRadius: `{radius_km:.2f} km` (`{radius_km * 0.621371:.2f} miles`)",
            "inline": False
        })
    elif state["type"] == "wedge":
        origin = state["coordinates"]["origin"]
        heading = state["coordinates"]["heading"]
        fg_rad_km = state["coordinates"]["foreground_radius"] / 1000.0
        bg_rad_km = state["coordinates"]["background_radius"] / 1000.0
        fov_angle = state["coordinates"]["fov_angle"]
        focal_length = state["coordinates"].get("focal_length", "N/A")
        embed_fields.append({
            "name": "Monitored Region (Camera FOV Wedge)",
            "value": (
                f"Origin: `{origin[0]:.4f}, {origin[1]:.4f}`\n"
                f"Heading: `{heading:.1f}°` | Focal Length: `{focal_length}mm` (FOV: `{fov_angle:.1f}°`)\n"
                f"Range: `{fg_rad_km:.2f} to {bg_rad_km:.2f} km` (`{fg_rad_km * 0.621371:.2f} to {bg_rad_km * 0.621371:.2f} miles`)"
            ),
            "inline": False
        })
    else:
        embed_fields.append({
            "name": "Monitored Region",
            "value": f"Polygon Area with {len(state['coordinates']['polygon'])} coordinates.",
            "inline": False
        })

    # List details for up to 6 closest strikes
    strikes_details = []
    for i, s in enumerate(strikes_sorted[:6]):
        lat, lon = s["lat"], s["lon"]
        dist_km = s["distance_meters"] / 1000.0 if s["distance_meters"] is not None else None
        dist_str = f" ({dist_km:.2f} km away)" if dist_km is not None else ""
        map_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        intensity_str = f"Intensity: `{s['intensity']} kA`" if s["intensity"] is not None else "Intensity: unknown"
        type_str = f"Type: `{s['type']}`"
        strikes_details.append(
            f"⚡ **Strike #{i+1}**{dist_str}\n"
            f"📍 [{lat:.4f}, {lon:.4f}]({map_url}) | {type_str} | {intensity_str}"
        )
    
    if count > 6:
        strikes_details.append(f"... and {count - 6} more strikes.")

    embed_fields.append({
        "name": "Strike Details",
        "value": "\n".join(strikes_details),
        "inline": False
    })
    
    payload = {
        "embeds": [{
            "title": "⚡ Flash Finder Alert",
            "description": desc,
            "color": color_val,
            "fields": embed_fields,
            "footer": {
                "text": "Flash Finder"
            },
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }]
    }
    
    await send_discord_message(payload)

async def check_lightning():
    if not state["active"]:
        return

    # Check timeout expiration
    now = datetime.datetime.now(datetime.timezone.utc)
    end_time_utc = state["end_time"]
    if end_time_utc and now >= end_time_utc:
        add_log("Monitoring session expired.")
        # Send Discord expiration alert
        await send_discord_message({
            "embeds": [{
                "title": "⏹️ Lightning Monitoring Stopped",
                "description": "The configured monitoring period has ended.",
                "color": 5859697,  # dark slate color
                "fields": [
                    {"name": "Duration", "value": f"{state['timeout_hours']} hours", "inline": True},
                    {"name": "Checks Run", "value": f"{state['checks_count']}", "inline": True},
                    {"name": "Strikes Detected", "value": f"{state['detections_count']}", "inline": True}
                ],
                "timestamp": now.isoformat()
            }]
        })
        state["active"] = False
        return

    state["checks_count"] += 1
    add_log(f"Check #{state['checks_count']} running...")

    # Load credentials
    config = load_config()
    xweather_key = config.get("xweather_api_key", "")
    client_id = ""
    client_secret = ""
    if "_" in xweather_key:
        parts = xweather_key.split("_", 1)
        client_id = parts[0]
        client_secret = parts[1]
    else:
        client_id = xweather_key

    if not client_id:
        err = "XWeather credentials missing in config or env."
        add_log(f"Error: {err}")
        state["last_error"] = err
        return

    # Build geographical query parameters
    try:
        # Determine center, query radius
        if state["type"] == "circle":
            center_lat, center_lon = state["coordinates"]["center"]
            radius_meters = state["coordinates"]["radius"]
            radius_miles = radius_meters / 1609.34
            # Enforce min 1 mile, max 60 miles for safety/caps
            radius_miles_query = max(1.0, min(radius_miles, 60.0))
        elif state["type"] == "wedge":
            origin = state["coordinates"]["origin"]
            center_lat, center_lon = origin[0], origin[1]
            radius_meters = state["coordinates"]["background_radius"]
            radius_miles = radius_meters / 1609.34
            # Enforce min 1 mile, max 60 miles for safety/caps
            radius_miles_query = max(1.0, min(radius_miles, 60.0))
        else: # polygon
            poly = state["coordinates"]["polygon"]
            # Centroid
            center_lat = sum(pt[0] for pt in poly) / len(poly)
            center_lon = sum(pt[1] for pt in poly) / len(poly)
            # Bounding radius
            max_dist = max(haversine_distance(center_lat, center_lon, pt[0], pt[1]) for pt in poly)
            radius_meters = max_dist
            radius_miles = radius_meters / 1609.34
            radius_miles_query = max(1.0, min(radius_miles, 60.0))

        strikes = []
        success = False
        error_msg = ""

        # Query XWeather /lightning/closest (Radius-based query available on standard/free tiers)
        async with httpx.AsyncClient() as client:
            url = "https://data.api.xweather.com/lightning/closest"
            params = {
                "p": f"{center_lat},{center_lon}",
                "radius": f"{radius_miles_query}mi",
                "limit": 1000,
                "client_id": client_id,
                "client_secret": client_secret
            }

            try:
                resp = await client.get(url, params=params, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        strikes = data.get("response", [])
                        success = True
                    else:
                        error_msg = data.get("error", {}).get("description", "Unknown API error")
                else:
                    error_msg = f"HTTP status {resp.status_code}"
            except Exception as e:
                error_msg = str(e)

        if not success:
            err = f"XWeather API error: {error_msg}"
            add_log(f"API query failed: {error_msg}")
            state["last_error"] = err
            return

        # Clear error state on successful API query
        state["last_error"] = None

        # Process strikes
        detected_in_this_run = []
        for strike in strikes:
            ob = strike.get("ob", {})
            strike_id = strike.get("id")
            if not strike_id:
                # generate pseudo id
                loc = strike.get("loc", {})
                ts = ob.get("timestamp") or strike.get("timestamp") or "0"
                strike_id = f"{ts}_{loc.get('lat')}_{loc.get('long')}"

            # Skip already seen strikes
            if strike_id in state["seen_strike_ids"]:
                continue

            # Geofilter check
            loc = strike.get("loc", {})
            strike_lat = loc.get("lat")
            strike_lon = loc.get("long")
            if strike_lat is None or strike_lon is None:
                continue

            dist_meters = haversine_distance(strike_lat, strike_lon, center_lat, center_lon)
            is_inside = False
            if state["type"] == "circle":
                if dist_meters <= radius_meters:
                    is_inside = True
            elif state["type"] == "wedge":
                origin = state["coordinates"]["origin"]
                fg_rad = state["coordinates"]["foreground_radius"]
                bg_rad = state["coordinates"]["background_radius"]
                heading = state["coordinates"]["heading"]
                fov_angle = state["coordinates"]["fov_angle"]
                if fg_rad <= dist_meters <= bg_rad:
                    strike_bearing = calculate_bearing(origin[0], origin[1], strike_lat, strike_lon)
                    start_angle = (heading - fov_angle / 2) % 360
                    sweep = fov_angle
                    diff = (strike_bearing - start_angle) % 360
                    if diff <= sweep:
                        is_inside = True
            else: # polygon
                poly = state["coordinates"]["polygon"]
                if is_point_in_polygon(strike_lat, strike_lon, poly):
                    is_inside = True

            strike_info = {
                "id": strike_id,
                "lat": strike_lat,
                "lon": strike_lon,
                "type": ob.get("type") or strike.get("type") or "Unknown",
                "intensity": ob.get("intensity") or strike.get("intensity"),
                "dateTimeISO": ob.get("dateTimeISO") or strike.get("dateTimeISO"),
                "timestamp": ob.get("timestamp") or strike.get("timestamp"),
                "distance_meters": dist_meters,
                "is_inside": is_inside,
                "detected_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            
            detected_in_this_run.append(strike_info)
            state["seen_strike_ids"].add(strike_id)
            # Keep cache bounded
            if len(state["seen_strike_ids"]) > 5000:
                state["seen_strike_ids"] = set(list(state["seen_strike_ids"])[-2500:])

        # Filter: save all strikes for plotting, but alert only on those inside
        alert_strikes = [s for s in detected_in_this_run if s["is_inside"]] if detected_in_this_run else []
        target_count = len(alert_strikes)
        
        if alert_strikes:
            state["detections_count"] += target_count

        if detected_in_this_run:
            state["recent_strikes"].extend(detected_in_this_run)
            state["recent_strikes"] = state["recent_strikes"][-10000:]

        # Log detailed check summary statistics
        total_api_strikes = len(strikes)
        new_strikes = len(detected_in_this_run)
        
        summary_log = (
            f"📊 Check #{state['checks_count']} Summary: "
            f"{total_api_strikes} total strike(s) in API buffer ({radius_miles_query:.1f}mi radius) | "
            f"{new_strikes} new | "
            f"{target_count} inside target area | "
            f"Session Total: {state['detections_count']}"
        )
        add_log(summary_log)

        if alert_strikes:
            add_log(f"⚡ ALERT: {target_count} strike(s) detected inside target region!")
            await trigger_discord_alert(alert_strikes)

    except Exception as e:
        logger.exception("Error checking lightning data:")
        add_log(f"Error during check: {e}")

async def tracking_loop():
    logger.info("Background tracking loop started.")
    try:
        while state["active"]:
            # Run check
            await check_lightning()
            # Wait based on configurable interval
            interval_seconds = int(state.get("interval_minutes", 5.0) * 60)
            for _ in range(int(interval_seconds / 5)):
                if not state["active"]:
                    break
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        logger.info("Background tracking loop cancelled.")
    except Exception as e:
        logger.exception("Error in background tracking loop:")
        add_log(f"Background loop crashed: {e}")
        state["active"] = False

@app.post("/api/start")
async def start_monitoring(req: StartTrackingRequest):
    global tracking_task
    
    if req.timeout_hours < 1.0 or req.timeout_hours > 4.0:
        raise HTTPException(status_code=400, detail="Timeout must be between 1 and 4 hours.")
        
    if req.interval_minutes < 1.0 or req.interval_minutes > 5.0:
        raise HTTPException(status_code=400, detail="Interval must be between 1 and 5 minutes.")
        
    if req.type not in ["circle", "polygon", "wedge"]:
        raise HTTPException(status_code=400, detail="Type must be 'circle', 'polygon', or 'wedge'.")
        
    # Cancel existing task if any
    if tracking_task and not tracking_task.done():
        state["active"] = False
        tracking_task.cancel()
        try:
            await tracking_task
        except asyncio.CancelledError:
            pass

    # Initialize state
    now = datetime.datetime.now(datetime.timezone.utc)
    end_time = now + datetime.timedelta(hours=req.timeout_hours)
    
    state["active"] = True
    state["start_time"] = now
    state["end_time"] = end_time
    state["timeout_hours"] = req.timeout_hours
    state["interval_minutes"] = req.interval_minutes
    state["type"] = req.type
    state["coordinates"] = req.coordinates
    state["checks_count"] = 0
    state["detections_count"] = 0
    state["recent_strikes"] = []
    state["logs"] = []
    state["seen_strike_ids"] = set()
    
    add_log(f"Started monitoring. Mode: {req.type.upper()}. Timeout: {req.timeout_hours} hours. Interval: {req.interval_minutes} minutes.")
    
    # Send Discord notification about starting
    await send_discord_message({
        "embeds": [{
            "title": "▶️ Lightning Monitoring Started",
            "description": f"Now monitoring the selected region for lightning strikes.",
            "color": 12702465,  # primary lime green
            "fields": [
                {"name": "Mode", "value": req.type.capitalize(), "inline": True},
                {"name": "Duration", "value": f"{req.timeout_hours} hours", "inline": True},
                {"name": "Expires (UTC)", "value": end_time.strftime("%Y-%m-%d %H:%M:%S"), "inline": False}
            ],
            "timestamp": now.isoformat()
        }]
    })

    # Start task
    tracking_task = asyncio.create_task(tracking_loop())
    
    return {"status": "started", "end_time": end_time.isoformat()}

@app.post("/api/stop")
async def stop_monitoring():
    global tracking_task
    if not state["active"]:
        return {"status": "already_stopped"}
        
    state["active"] = False
    if tracking_task:
        tracking_task.cancel()
        try:
            await tracking_task
        except asyncio.CancelledError:
            pass
        tracking_task = None
        
    add_log("Monitoring stopped manually by user.")
    
    # Send Discord notification about stopping
    now = datetime.datetime.now(datetime.timezone.utc)
    await send_discord_message({
        "embeds": [{
            "title": "⏹️ Lightning Monitoring Stopped",
            "description": "Monitoring was stopped manually by the user.",
            "color": 16143645,  # danger orange
            "fields": [
                {"name": "Checks Run", "value": f"{state['checks_count']}", "inline": True},
                {"name": "Strikes Detected", "value": f"{state['detections_count']}", "inline": True}
            ],
            "timestamp": now.isoformat()
        }]
    })
    
    return {"status": "stopped"}

@app.get("/api/status")
async def get_status():
    now = datetime.datetime.now(datetime.timezone.utc)
    time_remaining = None
    if state["active"] and state["end_time"]:
        remaining = (state["end_time"] - now).total_seconds()
        time_remaining = max(0.0, remaining)
        
    start_str = state["start_time"].isoformat() if state["start_time"] else None
    end_str = state["end_time"].isoformat() if state["end_time"] else None
    
    return {
        "active": state["active"],
        "start_time": start_str,
        "end_time": end_str,
        "timeout_hours": state["timeout_hours"],
        "interval_minutes": state.get("interval_minutes", 5.0),
        "type": state["type"],
        "coordinates": state["coordinates"],
        "time_remaining_seconds": time_remaining,
        "checks_count": state["checks_count"],
        "detections_count": state["detections_count"],
        "recent_strikes": state["recent_strikes"],
        "logs": state["logs"]
    }

class ConfigUpdateRequest(BaseModel):
    xweather_api_key: str
    discord_webhook: str

@app.get("/api/config")
async def get_config():
    config = load_config()
    
    def mask_key(k: str) -> str:
        if not k:
            return ""
        if len(k) <= 12:
            return "****"
        return f"{k[:6]}...{k[-6:]}"
        
    def mask_webhook(url: str) -> str:
        if not url:
            return ""
        if "/api/webhooks/" in url:
            parts = url.split("/api/webhooks/")
            webhook_parts = parts[1].split("/")
            webhook_id = webhook_parts[0]
            return f"https://discord.com/api/webhooks/{webhook_id}/*****"
        return f"{url[:15]}..."

    return {
        "xweather_configured": bool(config["xweather_api_key"]),
        "discord_configured": bool(config["discord_webhook"]),
        "xweather_api_key_masked": mask_key(config["xweather_api_key"]),
        "discord_webhook_masked": mask_webhook(config["discord_webhook"])
    }

@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest):
    if not os.path.exists(DATA_DIR) and DATA_DIR == "/app/data":
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create data dir {DATA_DIR}: {e}")

    current_config = load_config()
    
    xweather_key = req.xweather_api_key.strip()
    if "..." in xweather_key or xweather_key == "****" or not xweather_key:
        if xweather_key == "":
            xweather_key = ""
        else:
            xweather_key = current_config.get("xweather_api_key", "")
            
    webhook = req.discord_webhook.strip()
    if "/*****" in webhook or not webhook:
        if webhook == "":
            webhook = ""
        else:
            webhook = current_config.get("discord_webhook", "")

    config = {
        "xweather_api_key": xweather_key,
        "discord_webhook": webhook
    }
    
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4)
        add_log("Configuration saved successfully via UI.")
        return {"status": "success", "message": "Configuration saved successfully."}
    except Exception as e:
        logger.error(f"Failed to save configuration: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {e}")

@app.post("/api/test-discord")
async def test_discord():
    now = datetime.datetime.now(datetime.timezone.utc)
    success = await send_discord_message({
        "embeds": [{
            "title": "🔔 Discord Webhook Test",
            "description": "This is a test notification from the Flash Finder. If you see this, your webhook configuration is working!",
            "color": 11394297,  # secondary cyan (#adfcf9)
            "timestamp": now.isoformat()
        }]
    })
    if success:
        return {"status": "success", "message": "Test notification sent successfully."}
    else:
        raise HTTPException(status_code=500, detail="Failed to send test notification. Check server logs.")

# Serve Vue Static Files
frontend_dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")

if os.path.exists(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="assets")
    
    @app.get("/{fallback_path:path}")
    async def serve_frontend(fallback_path: str):
        file_path = os.path.join(frontend_dist, fallback_path)
        if fallback_path and os.path.exists(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(frontend_dist, "index.html"))
else:
    @app.get("/")
    async def root_placeholder():
        return JSONResponse(content={"message": "FastAPI is running. Frontend has not been built yet. Please run 'npm run build' inside the 'frontend' directory."})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
