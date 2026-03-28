"""
master-script.py
----------------
Tinkerer's Lab - Prototype 1 Entry System
Master script that ties together:
  - QrCodeHelper   : QR generation & validation
  - loggingDB      : SQLite entry logging
  - people_counter : YOLO-based physical entry detection (runs as a thread)

Flow:
  1. On startup, generates QR codes for all users in users.json and stores
     user records (name, au_id, role) into the DB.
  2. Mobile scans QR → hits GET /scan?token=... → validates token, confirms
     user exists in DB, then creates a 60 s auth request.
  3. people_counter thread detects physical entry → calls internal handler.
  4. Handler matches pending auth request → logs AUTHORISED entry.
     No match found                       → captures screenshot → logs UNAUTHORISED entry.
  5. Expired auth requests (QR scanned, no physical entry within 60 s) are
     logged as EXPIRED_AUTH when purged inside pop_valid_auth_request().

Run:
    python master-script.py

Optional flags:
    --users   path to users.json       (default: users.json)
    --source  camera index or URL      (default: 0)
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

import cv2
import uvicorn
import qrcode as qrc
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# ── Local modules ─────────────────────────────────────────────────────────────
from QrCodeHelper import LabAccessQrCode, AUColorMask, _finalize_qr
from loggingDB import log_entry, ensure_table, upsert_user, lookup_user
from people_counter import run_monitor_loop

# ── Config ────────────────────────────────────────────────────────────────────
PORT             = 8000
AUTH_EXPIRY_SECS = 60
USERS_FILE       = "users.json"
QR_OUTPUT_DIR    = "qr_codes"

# ── Shared state (thread-safe via lock) ───────────────────────────────────────
auth_requests: list[dict] = []   # {au_id, name, role, expires_at}
state_lock    = threading.Lock()

# ── Live frame cache (written by YOLO thread, read on unauthorised entry) ─────
_latest_frame       = None
_latest_frame_lock  = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_lan_ip() -> str:
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


def capture_screenshot() -> bytes | None:
    """
    Grab the most recent camera frame and encode it as JPEG bytes.
    Returns None if no frame is available yet.
    """
    with _latest_frame_lock:
        frame = _latest_frame
    if frame is None:
        return None
    success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buffer.tobytes() if success else None


def generate_all_qr_codes(users: list[dict], server_ip: str):
    """
    Generate one QR code per user and store user records in the DB.
    Role is read from users.json (falls back to 'student' if absent).
    """
    os.makedirs(QR_OUTPUT_DIR, exist_ok=True)
    qr_helper = LabAccessQrCode(expiration_minutes=AUTH_EXPIRY_SECS // 60)

    for user in users:
        au_id = user["AU_id"]
        name  = user["name"]
        role  = user.get("role", "student")   # read from users.json

        # Generate encrypted payload
        payload, _ = qr_helper.generate(name=name, enrolment_id=au_id)

        # ── Store user record in DB ──────────────────────────────────────────
        upsert_user(au_id=au_id, name=name, role=role, encrypted_payload=payload)

        # Build scan URL and re-encode QR with the full URL
        scan_url = f"http://{server_ip}:{PORT}/scan?token={payload}"

        qr = qrc.QRCode(
            error_correction=qrc.constants.ERROR_CORRECT_H,  # type: ignore
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
        print(f"  📄 QR saved: {out_path}  ({name}, {role})")


# ── Auth request pool ─────────────────────────────────────────────────────────

def create_auth_request(au_id: str, name: str, role: str):
    expires_at = datetime.now() + timedelta(seconds=AUTH_EXPIRY_SECS)
    with state_lock:
        # Remove any existing request for same user (re-scan case)
        auth_requests[:] = [r for r in auth_requests if r["au_id"] != au_id]
        auth_requests.append({
            "au_id":      au_id,
            "name":       name,
            "role":       role,
            "expires_at": expires_at,
        })
    print(f"  🔑 Auth request created: {name} ({au_id}, {role}) — expires in {AUTH_EXPIRY_SECS}s")


def pop_valid_auth_request() -> dict | None:
    """
    Remove and return the oldest non-expired auth request.
    Any expired requests found during cleanup are logged as EXPIRED_AUTH.
    Returns None if no valid request exists.
    """
    now = datetime.now()
    with state_lock:
        expired  = [r for r in auth_requests if r["expires_at"] <= now]
        valid    = [r for r in auth_requests if r["expires_at"] >  now]
        auth_requests[:] = valid

    # Log expired requests outside the lock to avoid holding it during DB I/O
    for r in expired:
        print(f"  ⏰ Auth request expired (no physical entry): {r['name']} ({r['au_id']})")
        log_entry(
            name=r["name"],
            au_id=r["au_id"],
            role=r["role"],
            people_count=0,
            status="EXPIRED_AUTH",
        )

    if valid:
        with state_lock:
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
            role=auth["role"],
            people_count=people_count,
            status="AUTHORISED",
        )
    else:
        print(f"\n🚨 [{timestamp}] UNAUTHORISED ENTRY DETECTED (people in frame: {people_count})")
        screenshot = capture_screenshot()
        log_entry(
            name="Unknown",
            au_id="N/A",
            role="student",       # schema requires non-null; unknown role defaults to student
            people_count=people_count,
            status="UNAUTHORISED",
            screenshot=screenshot,
        )
        if screenshot:
            print(f"  📸 Screenshot captured ({len(screenshot)} bytes) and stored in DB")
        else:
            print(f"  ⚠️  No screenshot available (frame not ready yet)")


# ── YOLO thread ───────────────────────────────────────────────────────────────

def _entry_callback(people_in_frame: int):
    """Called by run_monitor_loop on every line crossing."""
    handle_physical_entry(people_in_frame)


def _frame_cache_callback(frame) -> None:
    """Called by run_monitor_loop on every frame to keep _latest_frame fresh."""
    with _latest_frame_lock:
        global _latest_frame
        _latest_frame = frame.copy()


def run_people_counter_thread(source: int | str, line_y: int, visual: bool):
    """
    Runs the YOLO monitor loop, restarting automatically on any crash so the
    system stays live indefinitely. Only stops on KeyboardInterrupt.
    """
    while True:
        try:
            run_monitor_loop(
                source=source,
                line_y=line_y,
                visual=visual,
                on_entry_callback=_entry_callback,
                on_frame_callback=_frame_cache_callback,
            )
        except KeyboardInterrupt:
            print("\n🛑 Shutting down YOLO monitor.")
            break
        except Exception as e:
            print(f"❌ YOLO loop crashed: {e} — restarting in 3s...")
            time.sleep(3)


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    ensure_table()
    server_ip = get_lan_ip()
    app.state.server_ip = server_ip
    app.state.qr_helper = LabAccessQrCode(expiration_minutes=AUTH_EXPIRY_SECS // 60)

    print(f"\n🌐 Server IP  : {server_ip}")
    print(f"🔗 Scan URL   : http://{server_ip}:{PORT}/scan?token=<payload>")
    print(f"📋 Logs API   : http://{server_ip}:{PORT}/logs\n")

    users = load_users(args.users)
    print(f"👥 Loaded {len(users)} user(s) from {args.users}")
    print("Generating QR codes and syncing users to DB...\n")
    generate_all_qr_codes(users, server_ip)

    yield
    # Shutdown (nothing to clean up for now)


app = FastAPI(title="Tinkerer's Lab Entry System", lifespan=lifespan)


@app.get("/scan", response_class=HTMLResponse)
async def scan_qr(token: str):
    """
    Mobile browser hits this after scanning QR.
    1. Validates the encrypted token via QrCodeHelper.
    2. Confirms the user exists in the DB.
    3. Creates a 60 s auth request with the correct role.
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

    # ── DB lookup: confirm user is registered ────────────────────────────────
    user_record = lookup_user(au_id)
    if not user_record:
        return HTMLResponse(content=_render_page(
            title="Access Denied",
            emoji="🚫",
            message="Your QR code is valid but your account is not registered. Contact a lab admin.",
            colour="#b00020",
        ), status_code=403)

    role = user_record["role"]
    create_auth_request(au_id=au_id, name=name, role=role)

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
        SELECT id, name, au_id, role, people_count, status, timestamp
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
            "status":       r[5],
            "timestamp":    r[6],
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
                "role":       r["role"],
                "expires_in": round((r["expires_at"] - now).total_seconds()),
            }
            for r in auth_requests if r["expires_at"] > now
        ]
    return {"pending": active}


@app.get("/trigger")
async def trigger_entry(people: int = 1):
    """
    Testing endpoint — simulates a physical door crossing without a camera.
    Hit this after scanning a QR to test the full authorised/unauthorised flow.
    Example: GET /trigger or GET /trigger?people=2
    """
    handle_physical_entry(people_count=people)
    return {"simulated": True, "people_count": people}


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
    parser.add_argument("--users",  default=USERS_FILE, help="Path to users.json")
    parser.add_argument("--source", default="0",        help="Camera source (default: 0 for webcam)")
    parser.add_argument("--line-y", type=int, default=300, help="YOLO entry line Y-coordinate")
    parser.add_argument("--visual", action="store_true",   help="Show YOLO camera window")
    args = parser.parse_args()

    # macOS requires camera/OpenCV to run on the main thread.
    # Solution: uvicorn runs in a background daemon thread; YOLO runs on main thread.
    uvicorn_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT),
        daemon=True,
    )
    uvicorn_thread.start()

    # Give uvicorn a moment to bind and run lifespan startup (QR gen, DB init)
    time.sleep(3)

    # YOLO loop on main thread — required on macOS for camera access.
    # If the camera source is unavailable we fall back to an idle loop so the
    # FastAPI server (and /trigger endpoint) keeps running regardless.
    source = int(args.source) if str(args.source).isdigit() else args.source
    try:
        run_people_counter_thread(source, args.line_y, args.visual)
    except KeyboardInterrupt:
        pass
    finally:
        # Keep the process alive so the uvicorn daemon thread stays running.
        print("\n⚠️  YOLO monitor stopped — server still running. Press Ctrl+C again to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Server shut down.")
