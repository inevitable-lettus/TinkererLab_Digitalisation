"""
master-script.py
----------------
Tinkerer's Lab - Prototype 1 Entry System

Flow:
  1. On startup, syncs users from users.json into the DB.
  2. User navigates to /generate, enters name + AU_id.
     Backend validates against users.json, generates encrypted QR,
     stores it in qr_requests table, returns QR image.
  3. Mobile scans QR → hits GET /scan?token=...
     Backend atomically marks token as 'scanned' (prevents double-use),
     then creates a 60 s auth request.
  4. YOLO thread detects physical entry → calls handle_physical_entry().
  5. If people_count > 1: log UNAUTHORISED (tailgating).
     If auth request matches: log AUTHORISED, mark qr_request as 'used'.
     If no match: log UNAUTHORISED with screenshot.
  6. Expired auth requests (QR scanned, no entry within 60 s) are logged
     as EXPIRED_AUTH when purged in pop_valid_auth_request().

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
from io import BytesIO

import cv2
import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse

from QrCodeHelper import LabAccessQrCode, AUColorMask, _finalize_qr
from loggingDB import (
    log_entry, ensure_table, upsert_user, lookup_user,
    create_qr_request, get_qr_request, mark_qr_scanned_atomic,
    mark_qr_used, mark_qr_expired,
)
from people_counter import run_monitor_loop

PORT             = 8000
AUTH_EXPIRY_SECS = 60
QR_VALID_HOURS   = 24   # QR payload itself is valid for 24 hours
USERS_FILE       = "users.json"

# {au_id, name, role, expires_at, token} — token included so we can mark qr_request as used
auth_requests: list[dict] = []
state_lock = threading.Lock()

_latest_frame      = None
_latest_frame_lock = threading.Lock()


# ── Utilities ─────────────────────────────────────────────────────────────────

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


def validate_user_pair(name: str, au_id: str, users: list[dict]) -> dict | None:
    """
    Check if (name, au_id) pair exists in the loaded users list.
    Case-insensitive and strips whitespace so minor typos don't block access.
    """
    name_normalized  = name.strip().title()
    au_id_normalized = au_id.strip().upper()
    for user in users:
        if (user["name"].strip().title()  == name_normalized and
            user["AU_id"].strip().upper() == au_id_normalized):
            return user
    return None


def capture_screenshot() -> bytes | None:
    with _latest_frame_lock:
        frame = _latest_frame
    if frame is None:
        return None
    success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buffer.tobytes() if success else None


# ── Auth request pool ─────────────────────────────────────────────────────────

def create_auth_request(au_id: str, name: str, role: str, token: str):
    expires_at = datetime.now() + timedelta(seconds=AUTH_EXPIRY_SECS) #current time + number of seconds
    with state_lock:
        auth_requests[:] = [r for r in auth_requests if r["au_id"] != au_id] #new auth req created if au_id not found
        auth_requests.append({
            "au_id":      au_id,
            "name":       name,
            "role":       role,
            "token":      token,   # kept so we can update qr_requests.status on entry
            "expires_at": expires_at,
        })
    print(f"  🔑 Auth request created: {name} ({au_id}, {role}) — expires in {AUTH_EXPIRY_SECS}s")


def pop_valid_auth_request() -> dict | None:
    """
    Remove and return the oldest non-expired auth request.
    Expired requests are logged as EXPIRED_AUTH and their qr_request marked 'expired'.
    """
    now = datetime.now()
    with state_lock:
        expired  = [r for r in auth_requests if r["expires_at"] <= now]
        valid    = [r for r in auth_requests if r["expires_at"] >  now]
        auth_requests[:] = valid #only valid reqs left

    for r in expired:
        print(f"  ⏰ Auth expired (no entry): {r['name']} ({r['au_id']})")
        log_entry(name=r["name"], au_id=r["au_id"], role=r["role"],
                  people_count=0, status="EXPIRED_AUTH")
        mark_qr_expired(r["token"])

    if valid:
        with state_lock:
            if auth_requests:
                return auth_requests.pop(0)
    return None


# ── Physical entry handler (called by YOLO thread) ────────────────────────────

def handle_physical_entry(people_count: int):
    timestamp = datetime.now().strftime("%H:%M:%S")

    # Tailgating: more than one person → deny immediately
    # Tailgating should only be declared if the number of people entering is greater than the number of valid auth requests - NEEDS CHANGE
    now = datetime.now()
    with state_lock:
        valid_auth_count = len([r for r in auth_requests if r["expires_at"] > now])

    if people_count > 1 and people_count > valid_auth_count:
        print(f"\n❌ [{timestamp}] TAILGATING DETECTED ({people_count} people) — Denied")
        screenshot = capture_screenshot()
        log_entry(name="Unauthorized Group", au_id="N/A", role="student",
                  people_count=people_count, status="UNAUTHORISED", screenshot=screenshot)
        return

    auth = pop_valid_auth_request()

    if auth:
        print(f"\n✅ [{timestamp}] AUTHORISED — {auth['name']} ({auth['au_id']})")
        entry_id = log_entry(name=auth["name"], au_id=auth["au_id"], role=auth["role"],
                             people_count=1, status="AUTHORISED", qr_request_id=None)
        mark_qr_used(auth["token"], entry_id)
    else:
        print(f"\n🚨 [{timestamp}] UNAUTHORISED — No matching auth request")
        screenshot = capture_screenshot()
        log_entry(name="Unknown", au_id="N/A", role="student",
                  people_count=1, status="UNAUTHORISED", screenshot=screenshot)
        if screenshot:
            print(f"  📸 Screenshot captured ({len(screenshot)} bytes)")
        else:
            print(f"  ⚠️  No screenshot available")


# ── YOLO thread ───────────────────────────────────────────────────────────────

def _entry_callback(people_in_frame: int):
    handle_physical_entry(people_in_frame)


def _frame_cache_callback(frame):
    with _latest_frame_lock:
        global _latest_frame
        _latest_frame = frame.copy()


def run_people_counter_thread(source, line_y: int, visual: bool):
    while True:
        try:
            run_monitor_loop(
                source=source, line_y=line_y, visual=visual,
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
    ensure_table()
    server_ip = get_lan_ip()
    app.state.server_ip = server_ip
    app.state.qr_helper = LabAccessQrCode(expiration_minutes=QR_VALID_HOURS * 60)

    print(f"\n🌐 Server IP  : {server_ip}")
    print(f"🔗 Generate   : http://{server_ip}:{PORT}/generate")
    print(f"📋 Logs API   : http://{server_ip}:{PORT}/logs\n")

    users = load_users(args.users)
    app.state.users = users
    print(f"👥 Loaded {len(users)} user(s) from {args.users}")

    # Sync all users into DB so lookup_user() works at /scan time
    for user in users:
        upsert_user(au_id=user["AU_id"], name=user["name"], role=user.get("role", "student"))
    print("✅ Users synced to DB\n")

    yield


app = FastAPI(title="Tinkerer's Lab Entry System", lifespan=lifespan)


@app.get("/generate", response_class=HTMLResponse)
async def generate_page():
    """Shows the QR generation form."""
    return HTMLResponse(content=_render_generate_form())


@app.post("/generate")
async def generate_qr(name: str = Form(...), au_id: str = Form(...)):
    """
    Validates (name, au_id) against users.json, generates an encrypted QR,
    stores the request in qr_requests, and returns the QR image inline.
    """
    users = app.state.users
    user = validate_user_pair(name, au_id, users)

    if not user:
        return HTMLResponse(content=_render_page(
            title="Not Found",
            emoji="🚫",
            message="Name and AU ID don't match our records. Check your details.",
            colour="#b00020",
            extra_link=('<a href="/generate" style="color:#c9c0be;">← Try again</a>'),
        ), status_code=400)

    qr_helper: LabAccessQrCode = app.state.qr_helper
    server_ip = app.state.server_ip

#This is a fresh QR code generating script, can be replaced with existing code - NEEDS CHANGE
    # Generate token and QR image — payload encodes name + au_id + timestamp
    token, buffer = qr_helper.generate(
        name=user["name"], 
        enrolment_id=user["AU_id"],
        url_template=f"http://{server_ip}:{PORT}/scan?token={{token}}"
    )

    # Store in DB — expires_at is 24h from now (auth window is still 60 s post-scan)
    expires_at = datetime.now() + timedelta(hours=QR_VALID_HOURS)
    create_qr_request(au_id=user["AU_id"], name=user["name"],
                      token=token, expires_at=expires_at)

    print(f"  🖨️  QR generated for {user['name']} ({user['AU_id']})")

    return StreamingResponse(buffer, media_type="image/png",
                             headers={"Content-Disposition": "inline"})


@app.get("/scan", response_class=HTMLResponse)
async def scan_qr(token: str):
    """
    Mobile browser hits this after scanning QR.
    1. Looks up the token in qr_requests (stateful check).
    2. Atomically marks it as 'scanned' to prevent double-use.
    3. Validates the cryptographic payload.
    4. Creates a 60 s auth request.
    """
    # Stateful check first
    qr_req = get_qr_request(token)
    if not qr_req:
        return HTMLResponse(content=_render_page(
            title="Invalid QR",
            emoji="🚫",
            message="QR not recognised. Please generate a new one at the door terminal.",
            colour="#b00020",
        ), status_code=403)

    if qr_req["status"] != "pending":
        return HTMLResponse(content=_render_page(
            title="QR Already Used",
            emoji="⛔",
            message=f"This QR has already been used (status: {qr_req['status']}). Generate a new one.",
            colour="#b00020",
        ), status_code=403)

    # Atomic mark-as-scanned before doing anything else (prevents race condition)
    claimed = mark_qr_scanned_atomic(token)
    if not claimed:
        return HTMLResponse(content=_render_page(
            title="QR Already Used",
            emoji="⛔",
            message="This QR was just used by someone else. Please generate a new one.",
            colour="#b00020",
        ), status_code=403)

    # Cryptographic validation (checks AES-GCM integrity + 24h timestamp window)
    qr_helper: LabAccessQrCode = app.state.qr_helper
    is_valid, message = qr_helper.validate(token)
    if not is_valid:
        return HTMLResponse(content=_render_page(
            title="Access Denied",
            emoji="🚫",
            message=message,
            colour="#b00020",
        ), status_code=403)

    # Pull user data from qr_request row (already validated against users.json at generation)
    user_record = lookup_user(qr_req["au_id"])
    if not user_record:
        return HTMLResponse(content=_render_page(
            title="Access Denied",
            emoji="🚫",
            message="Account not registered. Contact a lab admin.",
            colour="#b00020",
        ), status_code=403)

    create_auth_request(
        au_id=user_record["au_id"],
        name=qr_req["name"],
        role=user_record["role"],
        token=token,
    )

    return HTMLResponse(content=_render_page(
        title="QR Validated",
        emoji="✅",
        message=f"Welcome, {qr_req['name']}!<br><small>Please walk through the door within {AUTH_EXPIRY_SECS} seconds.</small>",
        colour="#1b5e20",
    ), status_code=200)


@app.get("/logs")
async def get_logs():
    """Returns the 20 most recent log entries as JSON."""
    import sqlite3
    conn = sqlite3.connect("entry_logs.db")
    c = conn.cursor()
    c.execute("""
        SELECT id, name, au_id, role, people_count, status, qr_request_id, timestamp
        FROM entries
        ORDER BY timestamp DESC
        LIMIT 20
    """)
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "au_id": r[2], "role": r[3],
         "people_count": r[4], "status": r[5], "qr_request_id": r[6], "timestamp": r[7]}
        for r in rows
    ]


@app.get("/pending")
async def get_pending():
    """Dev endpoint — shows currently pending auth requests."""
    now = datetime.now()
    with state_lock:
        active = [
            {"au_id": r["au_id"], "name": r["name"], "role": r["role"],
             "expires_in": round((r["expires_at"] - now).total_seconds())}
            for r in auth_requests if r["expires_at"] > now
        ]
    return {"pending": active}


@app.get("/trigger")
async def trigger_entry(people: int = 1):
    """Testing endpoint — simulates a physical door crossing without a camera."""
    handle_physical_entry(people_count=people)
    return {"simulated": True, "people_count": people}


# ── HTML renderers ────────────────────────────────────────────────────────────

def _render_page(title: str, emoji: str, message: str, colour: str, extra_link: str = "") -> str:
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
        .link {{ margin-top: 1.2rem; font-size: 0.9rem; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="emoji">{emoji}</div>
        <h1>{title}</h1>
        <p>{message}</p>
        {f'<div class="link">{extra_link}</div>' if extra_link else ''}
      </div>
    </body>
    </html>
    """


def _render_generate_form() -> str:
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
      <title>Generate Entry QR</title>
      <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
          min-height: 100vh;
          display: flex; align-items: center; justify-content: center;
          background: #0e0a0a;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          color: #f5f0ee;
          padding: 2rem;
        }
        .card {
          background: #1a1414;
          border: 2px solid #85160f;
          border-radius: 16px;
          padding: 2.5rem 2rem;
          max-width: 360px;
          width: 100%;
          text-align: center;
        }
        h1 { font-size: 1.4rem; color: #85160f; margin-bottom: 0.4rem; }
        .sub { font-size: 0.85rem; color: #888; margin-bottom: 1.8rem; }
        label { display: block; text-align: left; font-size: 0.85rem;
                color: #aaa; margin-bottom: 0.3rem; }
        input {
          width: 100%; padding: 0.7rem 1rem;
          background: #0e0a0a; border: 1px solid #444;
          border-radius: 8px; color: #f5f0ee; font-size: 1rem;
          margin-bottom: 1.2rem; outline: none;
        }
        input:focus { border-color: #85160f; }
        button {
          width: 100%; padding: 0.8rem;
          background: #85160f; border: none; border-radius: 8px;
          color: #fff; font-size: 1rem; cursor: pointer;
          font-weight: 600; letter-spacing: 0.03em;
        }
        button:hover { background: #a01e15; }
      </style>
    </head>
    <body>
      <div class="card">
        <h1>🔐 Tinkerer's Lab</h1>
        <p class="sub">Enter your details to get a QR code for entry</p>
        <form method="post" action="/generate">
          <label for="name">Full Name</label>
          <input id="name" name="name" type="text" placeholder="Vansh Shah" required/>
          <label for="au_id">AU ID</label>
          <input id="au_id" name="au_id" type="text" placeholder="AU2540082" required/>
          <button type="submit">Generate QR</button>
        </form>
      </div>
    </body>
    </html>
    """


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tinkerer's Lab Master Script")
    parser.add_argument("--users",  default=USERS_FILE)
    parser.add_argument("--source", default="0")
    parser.add_argument("--line-y", type=int, default=300)
    parser.add_argument("--visual", action="store_true")
    args = parser.parse_args()

    uvicorn_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT),
        daemon=True,
    )
    uvicorn_thread.start()

    time.sleep(3)

    source = int(args.source) if str(args.source).isdigit() else args.source
    try:
        run_people_counter_thread(source, args.line_y, args.visual)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n⚠️  YOLO monitor stopped — server still running. Press Ctrl+C again to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Server shut down.")
