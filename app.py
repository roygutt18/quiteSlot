from flask import Flask, request, jsonify, render_template, session, redirect, abort
import datetime as dt
from zoneinfo import ZoneInfo
import json
from twilio.rest import Client
import os
import pickle
from dotenv import load_dotenv
load_dotenv()
import random
import re
from functools import wraps
from googleapiclient.errors import HttpError
import secrets
import hashlib
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from db import db
from models import Appointment, User, PhoneVerification, TrustedDevice
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from booking_core import (
    ceil_to_slot,
    validate_slot,
    day_key,
    get_working_hours_for_date,
)
app = Flask(__name__)

# ====== IMPORTANT: SECRET KEY (token signing) ======
# ב-Production תשים את זה במשתני סביבה.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me-please")

# ====== SESSION (login persistence) ======
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# בפרודקשן על HTTPS: תשים SESSION_COOKIE_SECURE=1 בסביבה
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(days=200)

# ====== DB ======
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "app.db")

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

with app.app_context():
    db.create_all()

# ================= CONFIG =================
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.pkl"
BUSINESS_CONFIG_FILE = "business_config.json"

DATA_DIR = os.path.join(BASE_DIR, "data")
ADMIN_OVERRIDES_FILE = os.path.join(DATA_DIR, "admin_overrides.json")
ADMIN_WHITELIST_FILE = os.path.join(DATA_DIR, "admin_whitelist.json")

# ====== rate limit (5 requests per 10 minutes) ======
_auth_requests = {} # key: identifier -> list of timestamps

def check_rate_limit(identifier: str) -> bool:
    now = dt.datetime.utcnow()
    ten_mins_ago = now - dt.timedelta(minutes=10)
    
    if identifier not in _auth_requests:
        _auth_requests[identifier] = []
    
    # Clean old requests
    _auth_requests[identifier] = [t for t in _auth_requests[identifier] if t > ten_mins_ago]
    
    if len(_auth_requests[identifier]) >= 5:
        return False
    
    _auth_requests[identifier].append(now)
    return True

def get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"])

def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D+", "", phone)
    # נפוץ בישראל: 10 ספרות עם 0 בהתחלה. נרשה 9-12 ספרות ל-MVP.
    if len(digits) < 9 or len(digits) > 12:
        return ""
    return digits

# ================= JSON helpers =================

def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default

def _atomic_write_json(path: str, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def deep_merge(base, override):
    """
    Dict deep merge.
    - dict merges recursively
    - lists/scalars replaced by override
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out

# ================= AUTH (customer) =================

def session_user():
    uid = session.get("user_id")
    if uid:
        u = User.query.get(uid)
        if u:
            return u

    # Check device cookie
    device_token = request.cookies.get("qs_device")
    if device_token:
        token_hash = hashlib.sha256(device_token.encode()).hexdigest()
        trusted = TrustedDevice.query.filter_by(device_token_hash=token_hash).first()
        if trusted and trusted.expires_at > dt.datetime.utcnow():
            # Refresh session
            session["user_id"] = trusted.user_id
            return User.query.get(trusted.user_id)
    return None

def require_login():
    """Return (user, error_response). error_response is a (response, status) tuple."""
    u = session_user()
    if not u:
        return None, (jsonify({"ok": False, "message": "לא מחובר"}), 401)
    return u, None

def require_phone_token(token: str, phone: str, max_age_sec: int = 600) -> bool:
    """Verify signed token matches phone and not expired. (legacy helper; kept for compatibility)."""
    if not token or not phone:
        return False
    s = get_serializer()
    try:
        data = s.loads(token, salt="phone-verify", max_age=max_age_sec)
    except (BadSignature, SignatureExpired):
        return False
    return data.get("phone") == phone

# ================= Admin Auth =================

def load_admin_whitelist() -> dict:
    return _read_json(ADMIN_WHITELIST_FILE, {})

def is_admin_phone_allowed(phone_digits: str):
    wl = load_admin_whitelist()
    rec = wl.get(phone_digits)
    if not rec:
        return False, []
    slugs = rec.get("slugs") or []
    slugs = [s for s in slugs if isinstance(s, str) and s.strip()]
    return True, slugs

def admin_session():
    phone = session.get("admin_phone")
    slugs = session.get("admin_slugs") or []
    if not phone or not isinstance(slugs, list) or not slugs:
        return None
    return {"phone": phone, "slugs": slugs}

def require_admin(slug: str):
    """
    Decorator for /admin/<slug>/ routes:
    - if not logged in -> redirect /admin/login?next=...
    - if logged in but slug not allowed -> 403
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            s = admin_session()
            if not s:
                next_url = request.path
                return redirect(f"/admin/login?next={next_url}")
            if slug not in s["slugs"]:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco

def admin_generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"

# ================= Business Config + Overrides =================

def load_business_config_map() -> dict:
    """
    Loads business_config.json and normalizes to:
    { "businesses": { "<slug>": { ...cfg... } } }
    Backward-compatible: if file is single-business dict, it becomes {"default": cfg}.
    """
    with open(BUSINESS_CONFIG_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    # Backward compatibility (old format: single business dict)
    if isinstance(raw, dict) and "businesses" not in raw:
        return {"businesses": {"default": raw}}

    if not isinstance(raw, dict) or "businesses" not in raw or not isinstance(raw["businesses"], dict):
        raise ValueError("Invalid business_config.json: expected {'businesses': {...}}")

    return raw

def load_admin_overrides_all() -> dict:
    return _read_json(ADMIN_OVERRIDES_FILE, {})

def save_admin_overrides_all(all_overrides: dict):
    _atomic_write_json(ADMIN_OVERRIDES_FILE, all_overrides)

def resolve_business_cfg(slug: str) -> dict:
    """Return business cfg for slug, merged with admin overrides."""
    slug = (slug or "").strip()
    if not slug:
        abort(404)

    cfg_map = load_business_config_map()["businesses"]
    base_cfg = cfg_map.get(slug)
    if not base_cfg:
        abort(404)

    base_cfg = dict(base_cfg)  # defensive copy
    base_cfg.setdefault("slug", slug)

    overrides_all = load_admin_overrides_all()
    override = overrides_all.get(slug) or {}

    merged = deep_merge(base_cfg, override)

    wh = merged.get("working_hours")
    if isinstance(wh, dict) and "default" in wh:
        wh.pop("start", None)
        wh.pop("end", None)

    merged.setdefault("slug", slug)
    return merged


# ================= Calendar =================

def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("calendar", "v3", credentials=creds)

def is_free(service, calendar_id, start_utc, end_utc):
    body = {
        "timeMin": start_utc.isoformat().replace("+00:00", "Z"),
        "timeMax": end_utc.isoformat().replace("+00:00", "Z"),
        "items": [{"id": calendar_id}],
    }
    fb = service.freebusy().query(body=body).execute()
    return not fb["calendars"][calendar_id]["busy"]

def add_event(service, calendar_id, start_local, end_local, name, phone, tz):
    event = service.events().insert(
        calendarId=calendar_id,
        body={
            "summary": f"תור - {name}",
            "description": f"טלפון: {phone}",
            "start": {"dateTime": start_local.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_local.isoformat(), "timeZone": tz},
        },
    ).execute()
    return event

# ================= Admin Routes =================

@app.route("/admin/login")
def admin_login():
    next_url = request.args.get("next") or "/admin/default/"
    # reset pending state
    session.pop("admin_pending_phone", None)
    session.pop("admin_pending_code", None)
    session.pop("admin_pending_exp", None)

    return render_template(
        "admin_login.html",
        step="phone",
        error=None,
        next_url=next_url
    )

@app.route("/admin/login/start", methods=["POST"])
def admin_login_start():
    next_url = request.form.get("next") or "/admin/default/"
    raw_phone = request.form.get("phone")
    phone = normalize_phone(raw_phone)

    if not phone:
        return render_template("admin_login.html", step="phone", error="טלפון לא תקין", next_url=next_url)

    ok, slugs = is_admin_phone_allowed(phone)
    if not ok:
        return render_template("admin_login.html", step="phone", error="טלפון לא מורשה", next_url=next_url)

    now = dt.datetime.utcnow()
    two_mins_ago = now - dt.timedelta(minutes=2)
    last_v = PhoneVerification.query.filter(
        PhoneVerification.phone == phone,
        PhoneVerification.created_at > two_mins_ago
    ).first()
    if last_v:
        return render_template("admin_login.html", step="phone", error="יש לחכות 2 דקות לפני בקשת קוד חדש", next_url=next_url)

    # Use PhoneVerification for consistency
    otp = str(random.randint(100000, 999999))
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    expires_at = now + dt.timedelta(minutes=10)

    PhoneVerification.query.filter_by(phone=phone).delete()
    v = PhoneVerification(phone=phone, code_hash=otp_hash, expires_at=expires_at, attempts=5)
    db.session.add(v)
    db.session.commit()

    # DEV MODE check
    if os.environ.get("ENV") == "DEV":
        print(f"\n[DEV MODE] ADMIN OTP for {phone}: {otp}\n")
    else:
        # Placeholder for delivery
        print(f"Sending ADMIN OTP {otp} to {phone}")

    session["admin_pending_phone"] = phone
    session["admin_pending_slugs"] = slugs

    return render_template("admin_login.html", step="otp", pending_phone=phone, next_url=next_url)

@app.route("/admin/login/verify", methods=["POST"])
def admin_login_verify():
    next_url = request.form.get("next") or "/admin/default/"
    phone = session.get("admin_pending_phone")
    code = (request.form.get("code") or "").strip()
    slugs = session.get("admin_pending_slugs")

    if not phone or not code:
        return redirect(f"/admin/login?next={next_url}")

    v = PhoneVerification.query.filter_by(phone=phone).order_by(PhoneVerification.created_at.desc()).first()
    
    if not v:
        return render_template("admin_login.html", step="phone", error="לא נמצאה בקשת אימות", next_url=next_url)

    if dt.datetime.utcnow() > v.expires_at:
        db.session.delete(v)
        db.session.commit()
        return render_template("admin_login.html", step="phone", error="הקוד פג תוקף", next_url=next_url)

    if v.attempts <= 0:
        db.session.delete(v)
        db.session.commit()
        return render_template("admin_login.html", step="phone", error="יותר מדי ניסיונות כושלים", next_url=next_url)

    # Decrease attempts
    v.attempts -= 1
    db.session.commit()

    code_hash = hashlib.sha256(code.encode()).hexdigest()
    if v.code_hash != code_hash:
        return render_template("admin_login.html", step="otp", pending_phone=phone, error=f"קוד שגוי. נשארו עוד {v.attempts} ניסיונות", next_url=next_url)

    # Success!
    db.session.delete(v)
    db.session.commit()

    # Admin session
    session.clear()
    session["admin_phone"] = phone
    session["admin_slugs"] = slugs
    session["admin_logged_in_at"] = dt.datetime.utcnow().isoformat()

    # redirect
    if next_url.startswith("/admin/"):
        return redirect(next_url)

    # default to first slug
    if slugs:
        return redirect(f"/admin/{slugs[0]}/")
    return redirect("/admin/login")

@app.route("/admin/logout")
def admin_logout():
    next_url = request.args.get("next") or "/admin/login"
    session.pop("admin_phone", None)
    session.pop("admin_slugs", None)
    session.pop("admin_logged_in_at", None)
    return redirect(next_url)

def _validate_time_hhmm(s: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", (s or "").strip()))

def _validate_date_iso(s: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (s or "").strip()):
        return False
    try:
        dt.date.fromisoformat(s.strip())
        return True
    except ValueError:
        return False

def _time_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def _validate_hours(start: str, end: str) -> bool:
    if not _validate_time_hhmm(start) or not _validate_time_hhmm(end):
        return False
    return _time_to_minutes(start) < _time_to_minutes(end)

def _normalize_working_hours_for_override(existing_wh, form_data):
    wh = existing_wh or {"default": {}, "by_day": {}}
    wh.setdefault("default", {})
    wh.setdefault("by_day", {})

    # ---- default ----
    ds = form_data.get("wh_default_start", "").strip()
    de = form_data.get("wh_default_end", "").strip()
    if ds and de:
        wh["default"]["start"] = ds
        wh["default"]["end"] = de

    # ---- per-day ----
    for dk in ["sun","mon","tue","wed","thu","fri","sat"]:
        s = form_data.get(f"wh_{dk}_start", "").strip()
        e = form_data.get(f"wh_{dk}_end", "").strip()
        b_raw = form_data.get(f"wh_{dk}_breaks", "").strip()

        day_exists = dk in wh["by_day"]
        day_cfg = wh["by_day"].setdefault(dk, {})

        # שעות ליום – אופציונלי
        if s and e:
            day_cfg["start"] = s
            day_cfg["end"] = e
        else:
            # אם לא ניתנו שעות – לא נוגעים בשעות (נופל ל-default)
            day_cfg.pop("start", None)
            day_cfg.pop("end", None)

        # breaks – תמיד מעדכנים (גם מחיקה)
        breaks = []
        for line in b_raw.splitlines():
            line = line.strip()
            if not line or "-" not in line:
                continue
            bs, be = [x.strip() for x in line.split("-", 1)]
            if _validate_hours(bs, be):
                breaks.append({"start": bs, "end": be})

        if breaks:
            day_cfg["breaks"] = breaks
        else:
            # 👈 זה התיקון למחיקה
            day_cfg.pop("breaks", None)

        # אם היום ריק לגמרי – ננקה אותו
        if not day_cfg:
            wh["by_day"].pop(dk, None)

    return wh



@app.route("/admin/<business_slug>/")
def admin_dashboard(business_slug):
    # protect
    s = admin_session()
    if not s:
        return redirect(f"/admin/login?next=/admin/{business_slug}/")
    if business_slug not in s["slugs"]:
        abort(403)

    cfg = resolve_business_cfg(business_slug)

    # precompute working hours for template
    # use today's date to extract default; for friday extract specifically
    wh = cfg.get("working_hours") or {}

    # default hours (לא תלוי ביום)
    if "start" in wh and "end" in wh:
        # legacy
        wh_default = {"start": wh["start"], "end": wh["end"]}
    else:
        wh_default = wh.get("default") or {"start": "09:00", "end": "17:00"}

    # friday override (אם קיים)
    wh_fri = {}
    if isinstance(wh.get("by_day"), dict):
        wh_fri = wh["by_day"].get("fri") or {}


    # build friday view: if by_day exists, show it, else empty
    wh = cfg.get("working_hours") or {}
    wh_fri = {}
    if isinstance(wh, dict):
        if "by_day" in wh and isinstance(wh.get("by_day"), dict):
            wh_fri = wh["by_day"].get("fri") or {}
        # legacy has no by_day

    closed_dates = cfg.get("closed_dates", []) or []
    closed_dates_text = "\n".join(closed_dates)

    services = cfg.get("services", []) or []

    return render_template(
        "admin_dashboard.html",
        business_slug=business_slug,
        cfg=cfg,
        display=cfg.get("display", {}),
        services=services,
        wh_default=wh_default,
        wh_fri=wh_fri,
        closed_dates_text=closed_dates_text,
        flash_ok=session.pop("admin_flash_ok", None),
        flash_err=session.pop("admin_flash_err", None),
    )

@app.route("/admin/<business_slug>/update", methods=["POST"])
def admin_update(business_slug):
    # protect
    s = admin_session()
    if not s:
        return redirect(f"/admin/login?next=/admin/{business_slug}/")
    if business_slug not in s["slugs"]:
        abort(403)

    cfg = resolve_business_cfg(business_slug)

    # ---- parse form ----
    display_name = (request.form.get("display_name") or "").strip()

    working_days = request.form.getlist("working_days")
    # allow only known keys
    valid_day_keys = {"sun","mon","tue","wed","thu","fri","sat"}
    working_days = [d for d in working_days if d in valid_day_keys]

    wh_default_start = (request.form.get("wh_default_start") or "").strip()
    wh_default_end = (request.form.get("wh_default_end") or "").strip()
    wh_fri_start = (request.form.get("wh_fri_start") or "").strip()
    wh_fri_end = (request.form.get("wh_fri_end") or "").strip()

    closed_dates_raw = (request.form.get("closed_dates") or "")
    closed_dates = []
    for line in closed_dates_raw.replace(",", "\n").splitlines():
        x = line.strip()
        if not x:
            continue
        if not _validate_date_iso(x):
            session["admin_flash_err"] = f"תאריך חסום לא תקין: {x}"
            return redirect(f"/admin/{business_slug}/")
        if x not in closed_dates:
            closed_dates.append(x)

    # services arrays
    svc_ids = request.form.getlist("svc_id")
    svc_names = request.form.getlist("svc_name")
    svc_durations = request.form.getlist("svc_duration")

    services = []
    for sid, sn, sd in zip(svc_ids, svc_names, svc_durations):
        sid = (sid or "").strip()
        sn = (sn or "").strip()
        sd = (sd or "").strip()

        if not sid and not sn and not sd:
            continue

        if not sid:
            session["admin_flash_err"] = "שירות חייב id"
            return redirect(f"/admin/{business_slug}/")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{2,40}", sid):
            session["admin_flash_err"] = f"id לא תקין לשירות: {sid}"
            return redirect(f"/admin/{business_slug}/")
        if not sn:
            session["admin_flash_err"] = f"שירות {sid} חייב name"
            return redirect(f"/admin/{business_slug}/")
        try:
            dur = int(sd)
        except ValueError:
            session["admin_flash_err"] = f"duration לא מספר עבור {sid}"
            return redirect(f"/admin/{business_slug}/")
        if dur <= 0 or dur > 600:
            session["admin_flash_err"] = f"duration לא תקין עבור {sid}"
            return redirect(f"/admin/{business_slug}/")

        services.append({"id": sid, "name": sn, "duration_minutes": dur})

    if not working_days:
        session["admin_flash_err"] = "חייב לבחור לפחות יום עבודה אחד"
        return redirect(f"/admin/{business_slug}/")

    if not _validate_hours(wh_default_start, wh_default_end):
        session["admin_flash_err"] = "שעות default לא תקינות (HH:MM, start<end)"
        return redirect(f"/admin/{business_slug}/")

    # friday override optional: either both empty, or both valid
    if (wh_fri_start or wh_fri_end):
        if not _validate_hours(wh_fri_start, wh_fri_end):
            session["admin_flash_err"] = "שעות שישי לא תקינות (או השאר ריק)"
            return redirect(f"/admin/{business_slug}/")

    # ---- build override payload (ONLY what admin edits) ----
    override = {}

    if display_name:
        override.setdefault("display", {})
        # merge to not kill other display keys
        override["display"]["name"] = display_name

    override["working_days"] = working_days
    override["closed_dates"] = closed_dates
    override["services"] = services

    # normalized working_hours schema in overrides
    override["working_hours"] = _normalize_working_hours_for_override(
        cfg.get("working_hours"),
        request.form
    )

    # ---- save overrides ----
    all_overrides = load_admin_overrides_all()
    all_overrides[business_slug] = override
    save_admin_overrides_all(all_overrides)

    session["admin_flash_ok"] = "המערכת עודכנה בהצלחה - השינויים נכנסו לתוקף"
    return redirect(f"/admin/{business_slug}/")

# ================= ROUTES (Customer) =================

@app.route("/")
def home():
    return redirect("/b/default/")

@app.route("/b/<slug>/")
def business_home(slug):
    cfg = resolve_business_cfg(slug)
    display = cfg.get("display", {})

    return render_template(
        "index.html",
        business_slug=slug,
        api_base=f"/b/{slug}",
        display=display
    )

@app.route("/api/me")
@app.route("/b/<slug>/api/me")
def api_me(slug="default"):
    u = session_user()
    if not u:
        return jsonify({"user": None})

    return jsonify({
        "user": {
            "id": u.id,
            "phone": u.phone,
            "email": u.email,
            "name": u.name,
            "logged_in_at": session.get("logged_in_at"),
        }
    })

@app.route("/api/profile", methods=["POST"])
@app.route("/b/<slug>/api/profile", methods=["POST"])
def api_profile(slug="default"):
    u, err = require_login()
    if err:
        return err

    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "שם חסר"}), 400

    u.name = name
    db.session.commit()
    return jsonify({"ok": True, "user": {"phone": u.phone, "name": u.name}})


def in_break(start_dt, end_dt, breaks):
    for b in breaks:
        b_start = dt.datetime.combine(
            start_dt.date(),
            dt.datetime.strptime(b["start"], "%H:%M").time(),
            tzinfo=start_dt.tzinfo
        )
        b_end = dt.datetime.combine(
            start_dt.date(),
            dt.datetime.strptime(b["end"], "%H:%M").time(),
            tzinfo=start_dt.tzinfo
        )
        if start_dt < b_end and b_start < end_dt:
            return True
    return False


@app.route("/api/day-slots")
@app.route("/b/<slug>/api/day-slots")
def api_day_slots(slug="default"):
    u, err = require_login()
    if err:
        return err

    # חסימה: אי אפשר לבחור תאריך בלי שם
    if not u.name:
        return jsonify({
            "ok": False,
            "code": "PROFILE_INCOMPLETE",
            "message": "יש להשלים שם לפני בחירת תאריך"
        }), 409

    cfg = resolve_business_cfg(slug)
    tz = ZoneInfo(cfg["timezone"])

    date_str = request.args.get("date")
    duration_raw = request.args.get("duration")

    if not duration_raw:
        return jsonify({"error": "missing service duration"}), 400
    try:
        duration = int(duration_raw)
    except ValueError:
        return jsonify({"error": "invalid duration"}), 400
    if duration <= 0:
        return jsonify({"error": "invalid duration"}), 400

    if not date_str:
        return jsonify({"error": "missing date"}), 400

    date = dt.date.fromisoformat(date_str)

    from booking_core import day_key, get_working_hours_for_date, ceil_to_slot

    if day_key(date) not in cfg["working_days"]:
        return jsonify({"slots": []})

    if date.isoformat() in cfg.get("closed_dates", []):
        return jsonify({"slots": []})

    # ✅ NEW: שעות עבודה לפי הסכימה החדשה (כולל שישי)
    wh = get_working_hours_for_date(cfg, date)
    start_h = dt.datetime.strptime(wh["start"], "%H:%M").time()
    end_h = dt.datetime.strptime(wh["end"], "%H:%M").time()
    breaks = wh.get("breaks", [])

    service = get_calendar_service()

    now_local = dt.datetime.now(tz)
    is_today = date == now_local.date()

    # אם היום כבר אחרי שעת סיום העבודה - אין שום סלוטים
    if is_today:
        end_dt = dt.datetime.combine(date, end_h, tzinfo=tz)
        if now_local >= end_dt:
            return jsonify({"slots": []})

    # === FETCH BUSY INTERVALS ===
    start_dt = dt.datetime.combine(date, start_h, tzinfo=tz)
    end_dt = dt.datetime.combine(date, end_h, tzinfo=tz)

    # 🔒 Start point: use working start or buffered "now"
    current_start = start_dt
    if is_today:
        # 10 minute buffer from "now"
        buffer_now = now_local + dt.timedelta(minutes=10)
        if buffer_now > current_start:
            current_start = buffer_now

    body = {
        "timeMin": start_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeMax": end_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": [{"id": cfg["calendar_id"]}],
    }
    fb = service.freebusy().query(body=body).execute()
    busy_list = fb["calendars"][cfg["calendar_id"]].get("busy", [])

    busy_intervals = []
    for b in busy_list:
        b_s = dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(tz)
        b_e = dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(tz)
        busy_intervals.append((b_s, b_e))

    # === PACK SLOTS BY DURATION ===
    slots = []

    cursor = current_start
    if is_today:
        cursor = ceil_to_slot(cursor, duration)
    else:
        cursor = dt.datetime.combine(date, start_h, tzinfo=tz)

    def overlaps(a_s, a_e, b_s, b_e):
        return a_s < b_e and b_s < a_e

    while cursor + dt.timedelta(minutes=duration) <= end_dt:
        cand_s = cursor
        cand_e = cursor + dt.timedelta(minutes=duration)

        if any(overlaps(cand_s, cand_e, b_s, b_e) for (b_s, b_e) in busy_intervals):
            cursor += dt.timedelta(minutes=duration)
            continue

        if not in_break(cand_s, cand_e, breaks):
            slots.append(cursor.strftime("%H:%M"))

        cursor += dt.timedelta(minutes=duration)

    return jsonify({"slots": slots})


# ====== AUTH: send code ======
# ================= AUTH: OTP (Phone) =================

@app.route("/api/auth/start", methods=["POST"])
@app.route("/b/<slug>/api/auth/start", methods=["POST"])
def auth_start(slug="default"):
    data = request.json or {}
    phone = normalize_phone(data.get("phone"))

    if not phone:
        return jsonify({"ok": False, "message": "מספר טלפון לא תקין"}), 400

    # Rate limit: max 1 code every 2 minutes per phone
    now = dt.datetime.utcnow()
    two_mins_ago = now - dt.timedelta(minutes=2)
    
    last_v = PhoneVerification.query.filter(
        PhoneVerification.phone == phone,
        PhoneVerification.created_at > two_mins_ago
    ).first()
    
    if last_v:
        return jsonify({"ok": False, "message": "יש לחכות 2 דקות בין בקשת קודים"}), 429

    # Generate 6 digit OTP
    otp = str(random.randint(100000, 999999))
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    expires_at = now + dt.timedelta(minutes=10)

    # Save to DB
    PhoneVerification.query.filter_by(phone=phone).delete()
    v = PhoneVerification(
        phone=phone,
        code_hash=otp_hash,
        expires_at=expires_at,
        attempts=5
    )
    db.session.add(v)
    db.session.commit()

    # DEV MODE check
    is_dev = os.environ.get("ENV") == "DEV"
    if is_dev:
        print(f"\n[DEV MODE] OTP for {phone}: {otp}\n")
    else:
        # Placeholder for WhatsApp/SMS delivery
        print(f"Sending OTP {otp} to {phone}")

    return jsonify({"ok": True})

@app.route("/api/auth/verify", methods=["POST"])
@app.route("/b/<slug>/api/auth/verify", methods=["POST"])
def auth_verify(slug="default"):
    data = request.json or {}
    phone = normalize_phone(data.get("phone"))
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()

    if not phone or not code:
        return jsonify({"ok": False, "message": "חסרים פרטים"}), 400

    v = PhoneVerification.query.filter_by(phone=phone).order_by(PhoneVerification.created_at.desc()).first()

    if not v:
        return jsonify({"ok": False, "message": "לא נמצאה בקשת אימות"}), 404

    if dt.datetime.utcnow() > v.expires_at:
        db.session.delete(v)
        db.session.commit()
        return jsonify({"ok": False, "message": "הקוד פג תוקף"}), 400

    if v.attempts <= 0:
        db.session.delete(v)
        db.session.commit()
        return jsonify({"ok": False, "message": "יותר מדי ניסיונות כושלים"}), 429

    # הורדת ניסיון
    v.attempts -= 1
    db.session.commit()

    # השוואת hash
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    if v.code_hash != code_hash:
        return jsonify({
            "ok": False,
            "message": "קוד שגוי",
            "attempts_left": v.attempts
        }), 401

    # הצלחה
    db.session.delete(v)

    user = User.query.filter_by(phone=phone).first()
    is_new = False

    if not user:
        is_new = True
        user = User(phone=phone, name=name if name else None)
        db.session.add(user)
        db.session.commit()
    elif name and not user.name:
        user.name = name
        db.session.commit()

    # ===== זכירת מכשיר ל-200 יום =====
    device_token = secrets.token_urlsafe(32)
    device_hash = hashlib.sha256(device_token.encode()).hexdigest()
    device_expiry = dt.datetime.utcnow() + dt.timedelta(days=200)

    td = TrustedDevice(
        user_id=user.id,
        device_token_hash=device_hash,
        expires_at=device_expiry
    )
    db.session.add(td)
    db.session.commit()

    # ===== Session =====
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["logged_in_at"] = dt.datetime.utcnow().isoformat()

    # ===== Response =====
    resp = jsonify({
        "ok": True,
        "user": {
            "id": user.id,
            "phone": user.phone,
            "name": user.name
        },
        "is_new": is_new and not user.name
    })

    resp.set_cookie(
        "qs_device",
        device_token,
        max_age=200 * 24 * 60 * 60,  # 200 ימים
        httponly=True,
        secure=app.config["SESSION_COOKIE_SECURE"],  # True בפרודקשן HTTPS
        samesite="Lax"
    )

    return resp


@app.route("/api/logout", methods=["POST"])
@app.route("/b/<slug>/api/logout", methods=["POST"])
def api_logout(slug="default"):
    """
    Clears the entire session.
    """
    session.clear()
    return jsonify({"success": True})

# ====== BOOK (requires login session + completed profile) ======
@app.route("/api/book", methods=["POST"])
@app.route("/b/<slug>/api/book", methods=["POST"])
def api_book(slug="default"):
    cfg = resolve_business_cfg(slug)
    tz = ZoneInfo(cfg["timezone"])
    data = request.json or {}

    date = data.get("date")
    time = data.get("time")
    duration_minutes = data.get("duration_minutes")

    if not duration_minutes:
        return jsonify({"error": "missing service duration"}), 400

    duration_minutes = int(duration_minutes)

    service_name = data.get("service_name", "תספורת")

    u, err = require_login()
    if err:
        return err

    # 🔒 חובה: שם חייב להיות שמור בפרופיל
    if not u.name:
        return jsonify({
            "ok": False,
            "code": "PROFILE_INCOMPLETE",
            "message": "יש להשלים שם לפני קביעת תור"
        }), 409

    phone = u.phone
    name = u.name

    if not phone:
        return jsonify({"ok": False, "message": "טלפון חסר"}), 400

    if not name:
        return jsonify({"ok": False, "message": "שם חסר"}), 400

    if not date or not time:
        return jsonify({"ok": False, "message": "חסרים תאריך או שעה"}), 400

    try:
        start_local = dt.datetime.strptime(
            f"{date} {time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=tz)
    except ValueError:
        return jsonify({"ok": False, "message": "תאריך או שעה לא תקינים"}), 400

    start_local = ceil_to_slot(start_local, 5) # Snap to 5 min instead of service duration
    end_local = start_local + dt.timedelta(minutes=duration_minutes)

    valid, msg = validate_slot(cfg, start_local, end_local)
    if not valid:
        return jsonify({"ok": False, "message": msg})

    service = get_calendar_service()
    if not is_free(
        service,
        cfg["calendar_id"],
        start_local.astimezone(dt.timezone.utc),
        end_local.astimezone(dt.timezone.utc),
    ):
        return jsonify({"ok": False, "message": "השעה תפוסה"})

    # ===== LIMIT FUTURE APPOINTMENTS PER USER =====
    MAX_ACTIVE_APPOINTMENTS = 4

    now = dt.datetime.utcnow()

    active_count = Appointment.query.filter(
        Appointment.phone == u.phone,
        Appointment.start_time >= now
    ).count()

    if active_count >= MAX_ACTIVE_APPOINTMENTS:
        return jsonify({
            "ok": False,
            "message": "ניתן לקבוע עד 4 תורים עתידיים לכל משתמש"
        }), 400


    # יצירת אירוע בלוחות
    event = add_event(
        service,
        cfg["calendar_id"],
        start_local,
        end_local,
        f"{name} - {service_name}",
        phone,
        cfg["timezone"]
    )

    appointment = Appointment(
        name=name,
        phone=phone,
        start_time=start_local,
        calendar_event_id=event["id"]
    )

    db.session.add(appointment)
    db.session.commit()

    return jsonify({"ok": True})

# ====== CANCEL LIST (requires login session) ======
@app.route("/api/cancel/list")
@app.route("/b/<slug>/api/cancel/list")
def api_cancel_list(slug="default"):
    u, err = require_login()
    if err:
        return err

    phone = u.phone
    if not phone:
        return jsonify({"appointments": []})

    appointments = Appointment.query.filter(
        Appointment.phone == phone
    ).order_by(Appointment.start_time.asc()).all()

    result = []
    for a in appointments:
        result.append({
            "id": a.id,
            "start": a.start_time.isoformat()
        })

    return jsonify({"appointments": result})

# ====== CANCEL (requires login session + phone match appointment) ======
@app.route("/api/cancel", methods=["POST"])
@app.route("/b/<slug>/api/cancel", methods=["POST"])
def api_cancel(slug="default"):
    data = request.json or {}
    appointment_id = data.get("id")

    u, err = require_login()
    if err:
        return err

    phone = u.phone

    if not appointment_id or not phone:
        return jsonify({"ok": False, "message": "חסר מזהה תור או טלפון"})

    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "תור לא נמצא"})

    # important: prevent cancelling other people's appointments
    if appointment.phone != phone:
        return jsonify({"ok": False, "message": "אין הרשאה לבטל את התור הזה"})

    service = get_calendar_service()
    cfg = resolve_business_cfg(slug)

    try:
        service.events().delete(
            calendarId=cfg["calendar_id"],
            eventId=appointment.calendar_event_id
        ).execute()
    except HttpError as e:
        # 410 = event already deleted → treat as success
        if e.resp.status != 410:
            raise

    db.session.delete(appointment)
    db.session.commit()

    return jsonify({"ok": True})

@app.route("/debug/db-count")
def db_count():
    return jsonify({"count": Appointment.query.count()})

@app.route("/b/<slug>/api/services")
def api_services(slug):
    cfg = resolve_business_cfg(slug)
    return jsonify({
        "services": cfg.get("services", []),
        "working_days": cfg.get("working_days", [])
    })



if __name__ == "__main__":
    print("APP.PY STARTED")
    app.run(host="0.0.0.0", port=5000)
