"""
master.py
---------
Tinkerer's Lab - Prototype 1 Entry System
Master script that ties together:
  - QrCodeHelper   : QR generation & validation
  - loggingDB      : SQLite entry logging
  - people_counter : YOLO-based physical entry detection (runs as a thread)

Flow:
  1. On startup, generates QR codes for all users in users.json
  2. Mobile scans QR → hits GET /scan?token=... → creates a 60s auth request
  3. people_counter thread detects physical entry → calls internal handler
  4. Handler matches pending auth request → logs authorised entry
     No match found                       → logs unauthorised entry

Run:
    python master.py

Optional flags:
    --users   path to users.json       (default: users.json)
    --line-y  Y-coordinate for YOLO    (default: 300)
    --visual  show YOLO camera window  (flag)
"""

import os
os.environ["YOLO_VERBOSE"] = "False"

import json
import re
import socket
import threading
import time
import argparse
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import uvicorn
import qrcode as qrc
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# ── Local modules ─────────────────────────────────────────────────────────────
from QrCodeHelper import LabAccessQrCode, AUColorMask, _finalize_qr
from loggingDB import log_entry, ensure_table

# ── Config ────────────────────────────────────────────────────────────────────
PORT             = 8000
AUTH_EXPIRY_SECS = 60
USERS_FILE       = "users.json"
QR_OUTPUT_DIR    = "qr_codes"

# ── Shared state (thread-safe via lock) ───────────────────────────────────────
auth_requests: list[dict] = []   # {au_id, name, expires_at}
state_lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_lan_ip() -> str:
    """Auto-detect the machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def load_users(filepath: str) -> list[dict]:
    with open(filepath, "r") as f:
        return json.load(f)["users"]


def parse_validate_message(message: str) -> tuple[str, str]:
    """
    Parse name and AU_id from QrCodeHelper.validate() success message.
    Format: "Access Granted for {name} (ID: {enrolment_id})."
    Returns: (name, au_id)
    """
    match = re.search(r"Access Granted for (.+?) \(ID: (.+?)\)\.", message)
    if match:
        return match.group(1), match.group(2)
    return "Unknown", "Unknown"


def generate_all_qr_codes(users: list[dict], server_ip: str):
    """Generate one QR code per user encoding a full scan URL."""
    import os
    os.makedirs(QR_OUTPUT_DIR, exist_ok=True)

    qr_helper = LabAccessQrCode(expiration_minutes=AUTH_EXPIRY_SECS // 60)

    for user in users:
        au_id = user["AU_id"]
        name  = user["name"]

        # Generate encrypted payload
        payload, image_buffer = qr_helper.generate(name=name, enrolment_id=au_id)

        # Build the scan URL and re-encode QR with the URL
        # The mobile browser will hit this URL directly
        scan_url = f"http://{server_ip}:{PORT}/scan?token={payload}"

        qr = qrc.QRCode(
            error_correction=qrc.constants.ERROR_CORRECT_H, # type: ignore
            box_size=12,
            border=2,
        )
        qr.add_data(scan_url)
        qr.make(fit=True)
        raw_img = qr.make_image(
            image_factory=StyledPilImage,
            module_drawer=RoundedModuleDrawer(radius_ratio=0.8),
            color_mask=AUColorMask(),
        )
        final_img = _finalize_qr(raw_img, None)

        out_path = os.path.join(QR_OUTPUT_DIR, f"{au_id}.png")
        final_img.save(out_path)
        print(f"  📄 QR saved: {out_path}  ({name})")


# ── Auth request pool ─────────────────────────────────────────────────────────

def create_auth_request(au_id: str, name: str):
    expires_at = datetime.now() + timedelta(seconds=AUTH_EXPIRY_SECS)
    with state_lock:
        # Remove any existing request for same user (re-scan case)
        auth_requests[:] = [r for r in auth_requests if r["au_id"] != au_id]
        auth_requests.append({
            "au_id":      au_id,
            "name":       name,
            "expires_at": expires_at,
        })
    print(f"  🔑 Auth request created: {name} ({au_id}) — expires in {AUTH_EXPIRY_SECS}s")


def pop_valid_auth_request() -> dict | None:
    """
    Remove and return the oldest non-expired auth request.
    Returns None if no valid request exists.
    """
    now = datetime.now()
    with state_lock:
        # Purge expired requests first
        auth_requests[:] = [r for r in auth_requests if r["expires_at"] > now]
        if auth_requests:
            return auth_requests.pop(0)
    return None


# ── Physical entry handler (called by YOLO thread) ────────────────────────────

def handle_physical_entry(people_count: int):
    """
    Called whenever YOLO detects a person crossing the entry line.
    Matches against pending auth requests and logs accordingly.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    auth = pop_valid_auth_request()

    if auth:
        print(f"\n✅ [{timestamp}] AUTHORISED ENTRY — {auth['name']} ({auth['au_id']})")
        log_entry(
            name=auth["name"],
            au_id=auth["au_id"],
            role="student",          # placeholder until role is in QR payload
            people_count=people_count,
        )
    else:
        print(f"\n🚨 [{timestamp}] UNAUTHORISED ENTRY DETECTED (people in frame: {people_count})")
        log_entry(
            name="Unknown",
            au_id="N/A",
            role="student",          # placeholder — schema requires non-null for now
            people_count=people_count,
        )


# ── YOLO thread ───────────────────────────────────────────────────────────────

def run_people_counter(source: int | str, line_y: int, visual: bool):
    """
    Runs people_counter logic in a background thread.
    Calls handle_physical_entry() when a crossing is detected.
    Adapted from people_counter.py.
    """
    import cv2
    from ultralytics import solutions

    region_points = [(100, 200), (500, 200), (500, 400), (100, 400)]

    trackzone = solutions.TrackZone(
        model="yolo11n.pt",
        region=region_points,
        show=visual,
        conf=0.5,
        classes=[0],
    )

    cap = cv2.VideoCapture(source)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print(f"❌ YOLO thread: Could not open camera source: {source}")
        return

    print(f"✅ YOLO monitor started (source={source}, line_y={line_y}, visual={visual})")

    prev_centers    = {}
    tracked_entries = set()
    entry_count     = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            results = trackzone(frame)

            if results is not None and hasattr(results, "plot_im"):
                annotated = results.plot_im
            elif results is not None and isinstance(results, list) and len(results) > 0:
                annotated = results[0].plot()
            else:
                annotated = frame

            if trackzone.track_ids is not None:
                people_in_frame = len(trackzone.track_ids)

                for i, track_id in enumerate(trackzone.track_ids):
                    if i < len(trackzone.boxes):
                        box = trackzone.boxes[i]
                        cx  = int((box[0] + box[2]) / 2)
                        cy  = int((box[1] + box[3]) / 2)

                        prev_cy = prev_centers.get(track_id)

                        if prev_cy is not None and track_id not in tracked_entries:
                            if prev_cy < line_y <= cy and 100 <= cx <= 500:
                                entry_count += 1
                                tracked_entries.add(track_id)
                                # ── Hand off to master handler ──
                                handle_physical_entry(people_in_frame)

                        prev_centers[track_id] = cy

            if visual:
                cv2.line(annotated, (100, line_y), (500, line_y), (0, 255, 0), 3)
                cv2.imshow("Door Monitor", annotated)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            time.sleep(0.01)

    except Exception as e:
        print(f"❌ YOLO thread crashed: {e}")
    finally:
        cap.release()
        if visual:
            cv2.destroyAllWindows()


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    ensure_table()
    server_ip = get_lan_ip()
    app.state.server_ip  = server_ip
    app.state.qr_helper  = LabAccessQrCode(expiration_minutes=AUTH_EXPIRY_SECS // 60)

    print(f"\n🌐 Server IP  : {server_ip}")
    print(f"🔗 Scan URL   : http://{server_ip}:{PORT}/scan?token=<payload>")
    print(f"📋 Logs API   : http://{server_ip}:{PORT}/logs\n")

    users = load_users(args.users)
    print(f"👥 Loaded {len(users)} user(s) from {args.users}")
    print("Generating QR codes...\n")
    generate_all_qr_codes(users, server_ip)

    # Start YOLO thread
    source = int(args.source) if str(args.source).isdigit() else args.source
    yolo_thread = threading.Thread(
        target=run_people_counter,
        args=(source, args.line_y, args.visual),
        daemon=True,
    )
    yolo_thread.start()

    yield
    # Shutdown (nothing to clean up for now)


app = FastAPI(title="Tinkerer's Lab Entry System", lifespan=lifespan)


@app.get("/scan", response_class=HTMLResponse)
async def scan_qr(token: str):
    """
    Mobile browser hits this after scanning QR.
    Validates the token and creates an auth request if valid.
    """
    qr_helper: LabAccessQrCode = app.state.qr_helper
    is_valid, message = qr_helper.validate(token)

    if not is_valid:
        return HTMLResponse(content=_render_page(
            title="Access Denied",
            emoji="🚫",
            message=message,
            colour="#b00020",
        ), status_code=403)

    name, au_id = parse_validate_message(message)
    create_auth_request(au_id=au_id, name=name)

    return HTMLResponse(content=_render_page(
        title="QR Validated",
        emoji="✅",
        message=f"Welcome, {name}!<br><small>Please walk through the door within {AUTH_EXPIRY_SECS} seconds.</small>",
        colour="#1b5e20",
    ), status_code=200)


@app.get("/logs")
async def get_logs():
    """Returns the 20 most recent log entries as JSON."""
    import sqlite3
    conn = sqlite3.connect("entry_logs.db")
    c = conn.cursor()
    c.execute("""
        SELECT id, name, au_id, role, people_count, timestamp
        FROM entries
        ORDER BY timestamp DESC
        LIMIT 20
    """)
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id":           r[0],
            "name":         r[1],
            "au_id":        r[2],
            "role":         r[3],
            "people_count": r[4],
            "timestamp":    r[5],
        }
        for r in rows
    ]


@app.get("/pending")
async def get_pending():
    """Dev endpoint — shows currently pending auth requests."""
    now = datetime.now()
    with state_lock:
        active = [
            {
                "au_id":      r["au_id"],
                "name":       r["name"],
                "expires_in": round((r["expires_at"] - now).total_seconds()),
            }
            for r in auth_requests if r["expires_at"] > now
        ]
    return {"pending": active}


# ── HTML response renderer ────────────────────────────────────────────────────

def _render_page(title: str, emoji: str, message: str, colour: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
      <title>{title}</title>
      <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
          min-height: 100vh;
          display: flex; align-items: center; justify-content: center;
          background: #0e0a0a;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          color: #f5f0ee;
          text-align: center;
          padding: 2rem;
        }}
        .card {{
          background: #1a1414;
          border: 2px solid {colour};
          border-radius: 16px;
          padding: 2.5rem 2rem;
          max-width: 340px;
          width: 100%;
        }}
        .emoji {{ font-size: 4rem; margin-bottom: 1rem; }}
        h1 {{ font-size: 1.5rem; color: {colour}; margin-bottom: 1rem; }}
        p  {{ font-size: 1rem; line-height: 1.6; color: #c9c0be; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="emoji">{emoji}</div>
        <h1>{title}</h1>
        <p>{message}</p>
      </div>
    </body>
    </html>
    """


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tinkerer's Lab Master Script")
    parser.add_argument("--users",   default=USERS_FILE,  help="Path to users.json")
    parser.add_argument("--source",  default="0",         help="Camera source (default: 0 for webcam)")
    parser.add_argument("--line-y",  type=int, default=300, help="YOLO entry line Y-coordinate")
    parser.add_argument("--visual",  action="store_true", help="Show YOLO camera window")
    args = parser.parse_args()

    uvicorn.run(app, host="0.0.0.0", port=PORT)