import os, sqlite3, secrets, base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import requests

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

load_dotenv()
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "jarrah_learning.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "dev-only-change-me-jarrah-learning"
app.config.update(
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

SQLITE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 role TEXT NOT NULL CHECK(role IN ('student','tutor','admin')),
 name TEXT NOT NULL,
 email TEXT UNIQUE NOT NULL,
 password_hash TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student_profiles(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 grade TEXT, subject TEXT, course_level TEXT, format_pref TEXT, tutor_gender_pref TEXT,
 goal TEXT, target TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS tutor_profiles(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 age INTEGER, subjects TEXT, highest_level TEXT, format TEXT, tutoring_type TEXT,
 qualifications TEXT, teaching_style TEXT, approved INTEGER NOT NULL DEFAULT 0,
 photo_path TEXT
);
CREATE TABLE IF NOT EXISTS availability(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 start_at TEXT NOT NULL, end_at TEXT NOT NULL, booked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assessments(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title TEXT NOT NULL, assessment_date TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS bookings(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 student_id INTEGER NOT NULL REFERENCES users(id), tutor_id INTEGER NOT NULL REFERENCES users(id),
 availability_id INTEGER REFERENCES availability(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 topic TEXT, strengths TEXT, weaknesses TEXT, status TEXT NOT NULL DEFAULT 'confirmed',
 zoom_join_url TEXT, zoom_start_url TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_files(
 id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
 uploader_id INTEGER NOT NULL REFERENCES users(id), filename TEXT NOT NULL, stored_name TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recaps(
 id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER UNIQUE NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
 tutor_id INTEGER NOT NULL REFERENCES users(id), summary TEXT NOT NULL, homework TEXT, created_at TEXT NOT NULL
);
"""

class DB:
    def __init__(self):
        if USE_POSTGRES:
            if not psycopg2:
                raise RuntimeError("psycopg2-binary is required when DATABASE_URL is configured")
            self.con = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, sslmode="require")
            self.pg = True
        else:
            self.con = sqlite3.connect(DB_PATH)
            self.con.row_factory = sqlite3.Row
            self.con.execute("PRAGMA foreign_keys=ON")
            self.pg = False

    def execute(self, sql, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
            cur = self.con.cursor()
            cur.execute(sql, params)
            return cur
        return self.con.execute(sql, params)

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()


def db():
    return DB()


def now_utc():
    return datetime.now(timezone.utc)


def init_db():
    if not USE_POSTGRES:
        con = sqlite3.connect(DB_PATH)
        con.executescript(SQLITE_SCHEMA)
        con.commit()
        con.close()
    con = db()
    email = os.getenv("ADMIN_EMAIL", "admin@example.com").lower()
    pw = os.getenv("ADMIN_PASSWORD", "ChangeThisPassword123!")
    pwh = generate_password_hash(pw)
    if USE_POSTGRES:
        con.execute(
            "INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET role='admin', password_hash=EXCLUDED.password_hash",
            ("admin", "Administrator", email, pwh, now_utc()),
        )
    else:
        existing = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not existing:
            con.execute(
                "INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",
                ("admin", "Administrator", email, pwh, now_utc().isoformat()),
            )
    con.commit()
    con.close()


init_db()


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", role=role or "student", next=request.path))
            if role and session.get("role") != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco


def current_user():
    if not session.get("user_id"):
        return None
    con = db()
    row = con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    con.close()
    return row


def safe_next(value):
    if not value:
        return None
    p = urlparse(value)
    return value if not p.netloc and not p.scheme else None


def score_match(student, tutor):
    score = 50
    ss = (student["subject"] or "").lower()
    ts = (tutor["subjects"] or "").lower()
    if ss and ss in ts:
        score += 25
    cl = (student["course_level"] or "").lower()
    hl = (tutor["highest_level"] or "").lower()
    if cl and any(x in hl for x in ["ib", "ap", "cegep", "college"] if x in cl):
        score += 8
    sf = (student["format_pref"] or "").lower()
    tf = (tutor["format"] or "").lower()
    if sf in tf or tf == "either" or sf == "either":
        score += 10
    pref = (student["tutor_gender_pref"] or "").lower()
    if "no preference" in pref or not pref:
        score += 5
    return min(score, 99)


def dt_for_zoom(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@app.template_filter("pretty_dt")
def pretty_dt(value):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value.replace("T", " ")
    return value.strftime("%a, %b %d · %I:%M %p").replace(" 0", " ")


def create_zoom_meeting(topic, start_at, duration=60):
    aid = os.getenv("ZOOM_ACCOUNT_ID")
    cid = os.getenv("ZOOM_CLIENT_ID")
    secret = os.getenv("ZOOM_CLIENT_SECRET")
    if not all([aid, cid, secret]):
        return None
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    tok = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": aid},
        headers={"Authorization": f"Basic {auth}"},
        timeout=15,
    )
    tok.raise_for_status()
    access = tok.json()["access_token"]
    host = os.getenv("ZOOM_HOST_USER_ID", "me")
    payload = {
        "topic": topic,
        "type": 2,
        "start_time": dt_for_zoom(start_at),
        "duration": duration,
        "timezone": "America/Toronto",
        "settings": {"waiting_room": True, "join_before_host": False},
    }
    r = requests.post(
        f"https://api.zoom.us/v2/users/{host}/meetings",
        json=payload,
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return {"join_url": data.get("join_url"), "start_url": data.get("start_url")}


def send_email(to, subject, html):
    key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("EMAIL_FROM", "Jarrah Learning <onboarding@resend.dev>")
    if not key or not to:
        return False
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"from": sender, "to": [to], "subject": subject, "html": html},
        timeout=15,
    )
    if not r.ok:
        app.logger.warning("Email failed (%s): %s", r.status_code, r.text[:300])
        return False
    return True


def send_booking_notifications(booking_id):
    con = db()
    b = con.execute(
        "SELECT b.*, s.name student_name, s.email student_email, t.name tutor_name, t.email tutor_email "
        "FROM bookings b JOIN users s ON s.id=b.student_id JOIN users t ON t.id=b.tutor_id WHERE b.id=?",
        (booking_id,),
    ).fetchone()
    con.close()
    if not b:
        return False
    when = pretty_dt(b["start_at"])
    topic = b["topic"] or "Not specified"
    app_url = os.getenv("APP_URL", "https://jarrah-learning.onrender.com")
    tutor_html = f"""
    <div style='font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#111'>
      <h2>New tutoring session booked 🎉</h2>
      <p><b>{b['student_name']}</b> just reserved a session with you.</p>
      <p><b>When:</b> {when}<br><b>Topic:</b> {topic}</p>
      <p><a href='{app_url}/tutor' style='display:inline-block;background:#111;color:#fff;padding:12px 18px;text-decoration:none;border-radius:999px'>Open tutor dashboard</a></p>
      <p style='color:#666;font-size:13px'>Jarrah Learning</p>
    </div>"""
    student_html = f"""
    <div style='font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#111'>
      <h2>Your session is booked ✓</h2>
      <p>You're booked with <b>{b['tutor_name']}</b>.</p>
      <p><b>When:</b> {when}<br><b>Topic:</b> {topic}</p>
      <p><a href='{app_url}/student' style='display:inline-block;background:#111;color:#fff;padding:12px 18px;text-decoration:none;border-radius:999px'>Open student dashboard</a></p>
      <p style='color:#666;font-size:13px'>Jarrah Learning</p>
    </div>"""
    tutor_sent = send_email(b["tutor_email"], f"New booking from {b['student_name']}", tutor_html)
    send_email(b["student_email"], f"Booking confirmed with {b['tutor_name']}", student_html)
    return tutor_sent


@app.get("/")
def home():
    return render_template("home.html", user=current_user())


@app.get("/health")
def health():
    return {"ok": True, "database": "supabase-postgres" if USE_POSTGRES else "local-sqlite"}


@app.route("/signup/student", methods=["GET", "POST"])
def signup_student():
    if request.method == "POST":
        f = request.form
        con = db()
        try:
            if USE_POSTGRES:
                cur = con.execute(
                    "INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?) RETURNING id",
                    ("student", f["name"], f["email"].lower(), generate_password_hash(f["password"]), now_utc()),
                )
                uid = cur.fetchone()["id"]
            else:
                cur = con.execute(
                    "INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",
                    ("student", f["name"], f["email"].lower(), generate_password_hash(f["password"]), now_utc().isoformat()),
                )
                uid = cur.lastrowid
            con.execute(
                "INSERT INTO student_profiles(user_id,grade,subject,course_level,format_pref,tutor_gender_pref,goal,target,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, f.get("grade"), f.get("subject"), f.get("course_level"), f.get("format_pref"),
                 f.get("tutor_gender_pref"), f.get("goal"), f.get("target"), f.get("notes")),
            )
            con.commit()
            session.clear()
            session.permanent = True
            session.update(user_id=uid, role="student")
            return redirect(url_for("student_dashboard"))
        except Exception as e:
            con.rollback()
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                flash("That email is already registered. Try logging in instead.", "error")
            else:
                app.logger.exception("Student signup failed")
                flash("We couldn't create your account. Please try again.", "error")
        finally:
            con.close()
    return render_template("signup_student.html")


@app.route("/signup/tutor", methods=["GET", "POST"])
def signup_tutor():
    if request.method == "POST":
        f = request.form
        con = db()
        try:
            if USE_POSTGRES:
                cur = con.execute(
                    "INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?) RETURNING id",
                    ("tutor", f["name"], f["email"].lower(), generate_password_hash(f["password"]), now_utc()),
                )
                uid = cur.fetchone()["id"]
            else:
                cur = con.execute(
                    "INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",
                    ("tutor", f["name"], f["email"].lower(), generate_password_hash(f["password"]), now_utc().isoformat()),
                )
                uid = cur.lastrowid
            photo = None
            up = request.files.get("photo")
            if up and up.filename:
                ext = Path(secure_filename(up.filename)).suffix.lower()
                if ext in {".jpg", ".jpeg", ".png", ".webp"}:
                    stored = f"tutor_{uid}_{secrets.token_hex(6)}{ext}"
                    up.save(UPLOAD_DIR / stored)
                    photo = stored
            con.execute(
                "INSERT INTO tutor_profiles(user_id,age,subjects,highest_level,format,tutoring_type,qualifications,teaching_style,photo_path) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, f.get("age") or None, f.get("subjects"), f.get("highest_level"), f.get("format"), f.get("tutoring_type"),
                 f.get("qualifications"), f.get("teaching_style"), photo),
            )
            con.commit()
            session.clear()
            session.permanent = True
            session.update(user_id=uid, role="tutor")
            return redirect(url_for("tutor_dashboard"))
        except Exception as e:
            con.rollback()
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                flash("That email is already registered. Try logging in instead.", "error")
            else:
                app.logger.exception("Tutor signup failed")
                flash("We couldn't create your account. Please try again.", "error")
        finally:
            con.close()
    return render_template("signup_tutor.html")


@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):
    if role not in {"student", "tutor", "admin"}:
        abort(404)
    if request.method == "POST":
        con = db()
        u = con.execute("SELECT * FROM users WHERE email=? AND role=?", (request.form["email"].lower(), role)).fetchone()
        con.close()
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session.permanent = True
            session.update(user_id=u["id"], role=u["role"])
            endpoint = {"student": "student_dashboard", "tutor": "tutor_dashboard", "admin": "admin_dashboard"}[role]
            return redirect(safe_next(request.args.get("next")) or url_for(endpoint))
        flash("Incorrect email or password.", "error")
    return render_template("login.html", role=role)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.get("/student")
@login_required("student")
def student_dashboard():
    uid = session["user_id"]
    con = db()
    s = con.execute("SELECT u.name,u.email,p.* FROM users u JOIN student_profiles p ON p.user_id=u.id WHERE u.id=?", (uid,)).fetchone()
    tutors = con.execute("SELECT u.id,u.name,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE p.approved=?", (True,)).fetchall()
    matches = sorted([(score_match(s, t), t) for t in tutors], key=lambda x: x[0], reverse=True)
    bookings = con.execute(
        "SELECT b.*,u.name tutor_name FROM bookings b JOIN users u ON u.id=b.tutor_id WHERE b.student_id=? ORDER BY b.start_at DESC", (uid,)
    ).fetchall()
    assessments = con.execute("SELECT * FROM assessments WHERE student_id=? ORDER BY assessment_date", (uid,)).fetchall()
    con.close()
    return render_template("student.html", profile=s, matches=matches, bookings=bookings, assessments=assessments)


@app.post("/student/assessment")
@login_required("student")
def add_assessment():
    con = db()
    con.execute(
        "INSERT INTO assessments(student_id,title,assessment_date,notes) VALUES(?,?,?,?)",
        (session["user_id"], request.form["title"], request.form["assessment_date"], request.form.get("notes")),
    )
    con.commit(); con.close()
    flash("Assessment added.", "success")
    return redirect(url_for("student_dashboard"))


@app.get("/student/tutor/<int:tutor_id>")
@login_required("student")
def tutor_detail(tutor_id):
    con = db()
    tutor = con.execute(
        "SELECT u.id,u.name,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE u.id=? AND p.approved=?",
        (tutor_id, True),
    ).fetchone()
    slots = con.execute(
        "SELECT * FROM availability WHERE tutor_id=? AND booked=? AND start_at>? ORDER BY start_at",
        (tutor_id, False, now_utc()),
    ).fetchall()
    con.close()
    if not tutor: abort(404)
    return render_template("tutor_detail.html", tutor=tutor, slots=slots)


@app.route("/student/book/<int:slot_id>", methods=["GET", "POST"])
@login_required("student")
def book(slot_id):
    con = db()
    slot = con.execute(
        "SELECT a.*,u.name tutor_name,p.tutoring_type FROM availability a JOIN users u ON u.id=a.tutor_id "
        "JOIN tutor_profiles p ON p.user_id=u.id WHERE a.id=? AND a.booked=?",
        (slot_id, False),
    ).fetchone()
    if not slot:
        con.close(); abort(404)
    if request.method == "POST":
        try:
            if USE_POSTGRES:
                cur = con.execute(
                    "INSERT INTO bookings(student_id,tutor_id,availability_id,start_at,end_at,topic,strengths,weaknesses,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
                    (session["user_id"], slot["tutor_id"], slot_id, slot["start_at"], slot["end_at"], request.form.get("topic"),
                     request.form.get("strengths"), request.form.get("weaknesses"), "confirmed", now_utc()),
                )
                bid = cur.fetchone()["id"]
            else:
                cur = con.execute(
                    "INSERT INTO bookings(student_id,tutor_id,availability_id,start_at,end_at,topic,strengths,weaknesses,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (session["user_id"], slot["tutor_id"], slot_id, slot["start_at"], slot["end_at"], request.form.get("topic"),
                     request.form.get("strengths"), request.form.get("weaknesses"), "confirmed", now_utc().isoformat()),
                )
                bid = cur.lastrowid
            for up in request.files.getlist("files"):
                if up and up.filename:
                    original = secure_filename(up.filename)
                    ext = Path(original).suffix.lower()
                    if ext in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".docx"}:
                        stored = f"booking_{bid}_{secrets.token_hex(7)}{ext}"
                        up.save(UPLOAD_DIR / stored)
                        con.execute(
                            "INSERT INTO booking_files(booking_id,uploader_id,filename,stored_name,kind) VALUES(?,?,?,?,?)",
                            (bid, session["user_id"], original, stored, "student_prep"),
                        )
            con.execute("UPDATE availability SET booked=? WHERE id=?", (True, slot_id))
            con.commit()
            try:
                z = create_zoom_meeting(f"Jarrah Learning tutoring: {slot['tutor_name']}", slot["start_at"])
                if z:
                    con.execute("UPDATE bookings SET zoom_join_url=?,zoom_start_url=? WHERE id=?", (z["join_url"], z["start_url"], bid))
                    con.commit()
            except Exception as e:
                app.logger.warning("Zoom creation failed: %s", e)
            con.close()
            emailed = send_booking_notifications(bid)
            flash("Session booked — your tutor has been emailed." if emailed else "Session booked.", "success")
            return redirect(url_for("student_dashboard"))
        except Exception:
            con.rollback(); con.close()
            app.logger.exception("Booking failed")
            flash("We couldn't complete the booking. Please try again.", "error")
            return redirect(url_for("tutor_detail", tutor_id=slot["tutor_id"]))
    con.close()
    return render_template("book.html", slot=slot)


@app.post("/booking/<int:booking_id>/cancel")
@login_required()
def cancel_booking(booking_id):
    uid, role = session["user_id"], session["role"]
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not b or (role == "student" and b["student_id"] != uid) or (role == "tutor" and b["tutor_id"] != uid):
        con.close(); abort(403)
    con.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    if b["availability_id"]:
        con.execute("UPDATE availability SET booked=? WHERE id=?", (False, b["availability_id"]))
    con.commit(); con.close()
    flash("Booking cancelled.", "success")
    return redirect(url_for(f"{role}_dashboard"))


@app.get("/tutor")
@login_required("tutor")
def tutor_dashboard():
    uid = session["user_id"]
    con = db()
    p = con.execute("SELECT u.*,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE u.id=?", (uid,)).fetchone()
    slots = con.execute("SELECT * FROM availability WHERE tutor_id=? ORDER BY start_at DESC LIMIT 30", (uid,)).fetchall()
    bookings = con.execute(
        "SELECT b.*,u.name student_name FROM bookings b JOIN users u ON u.id=b.student_id WHERE b.tutor_id=? ORDER BY b.start_at DESC", (uid,)
    ).fetchall()
    con.close()
    return render_template("tutor.html", profile=p, slots=slots, bookings=bookings)


@app.post("/tutor/availability")
@login_required("tutor")
def tutor_availability():
    start = datetime.fromisoformat(request.form["start_at"])
    if start.tzinfo is None:
        # Montreal/Toronto local input; storing an aware offset avoids ambiguous Postgres timestamps.
        from zoneinfo import ZoneInfo
        start = start.replace(tzinfo=ZoneInfo("America/Toronto"))
    end = start + timedelta(minutes=int(request.form.get("minutes", 60)))
    con = db()
    con.execute("INSERT INTO availability(tutor_id,start_at,end_at) VALUES(?,?,?)", (session["user_id"], start, end))
    con.commit(); con.close()
    flash("Availability added.", "success")
    return redirect(url_for("tutor_dashboard"))


@app.post("/tutor/booking/<int:booking_id>/recap")
@login_required("tutor")
def tutor_recap(booking_id):
    con = db()
    b = con.execute("SELECT * FROM bookings WHERE id=? AND tutor_id=?", (booking_id, session["user_id"])).fetchone()
    if not b:
        con.close(); abort(404)
    con.execute(
        "INSERT INTO recaps(booking_id,tutor_id,summary,homework,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(booking_id) DO UPDATE SET summary=EXCLUDED.summary,homework=EXCLUDED.homework,created_at=EXCLUDED.created_at",
        (booking_id, session["user_id"], request.form["summary"], request.form.get("homework"), now_utc()),
    )
    for up in request.files.getlist("files"):
        if up and up.filename:
            original = secure_filename(up.filename)
            ext = Path(original).suffix.lower()
            if ext in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".docx"}:
                stored = f"recap_{booking_id}_{secrets.token_hex(7)}{ext}"
                up.save(UPLOAD_DIR / stored)
                con.execute(
                    "INSERT INTO booking_files(booking_id,uploader_id,filename,stored_name,kind) VALUES(?,?,?,?,?)",
                    (booking_id, session["user_id"], original, stored, "tutor_notes"),
                )
    con.execute("UPDATE bookings SET status='completed' WHERE id=?", (booking_id,))
    con.commit(); con.close()
    flash("Recap saved and session completed.", "success")
    return redirect(url_for("tutor_dashboard"))

@app.get("/admin")
@login_required("admin")
def admin_dashboard():
    con = db()

    student_accounts = con.execute(
        "SELECT id, name, email, created_at FROM users "
        "WHERE role='student' ORDER BY name"
    ).fetchall()

    tutor_accounts = con.execute(
        "SELECT u.id, u.name, u.email, u.created_at, p.approved "
        "FROM users u JOIN tutor_profiles p ON p.user_id=u.id "
        "WHERE u.role='tutor' ORDER BY u.name"
    ).fetchall()

    students = len(student_accounts)
    tutors = len(tutor_accounts)

    pending = con.execute(
        "SELECT u.id,u.name,u.email,p.* FROM users u "
        "JOIN tutor_profiles p ON p.user_id=u.id WHERE p.approved=?",
        (False,),
    ).fetchall()

    bookings = con.execute(
        "SELECT b.*,s.name student_name,t.name tutor_name "
        "FROM bookings b "
        "JOIN users s ON s.id=b.student_id "
        "JOIN users t ON t.id=b.tutor_id "
        "ORDER BY b.start_at DESC LIMIT 100"
    ).fetchall()

    con.close()

    return render_template(
        "admin.html",
        students=students,
        tutors=tutors,
        student_accounts=student_accounts,
        tutor_accounts=tutor_accounts,
        pending=pending,
        bookings=bookings,
        db_backend="Supabase" if USE_POSTGRES else "Local",
    )


@app.post("/admin/tutor/<int:tutor_id>/approve")
@login_required("admin")
def approve_tutor(tutor_id):
    con = db()
    con.execute(
        "UPDATE tutor_profiles SET approved=? WHERE user_id=?",
        (True, tutor_id),
    )
    con.commit()
    con.close()

    flash("Tutor approved.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/user/<int:user_id>/delete")
@login_required("admin")
def delete_user(user_id):
    con = db()

    user = con.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,),
    ).fetchone()

    if not user or user["role"] == "admin":
        con.close()
        abort(403)

    try:
        # Re-open any booked availability slots connected to this user.
        booked_slots = con.execute(
            "SELECT availability_id FROM bookings "
            "WHERE (student_id=? OR tutor_id=?) "
            "AND availability_id IS NOT NULL",
            (user_id, user_id),
        ).fetchall()

        for slot in booked_slots:
            con.execute(
                "UPDATE availability SET booked=? WHERE id=?",
                (False, slot["availability_id"]),
            )

        # Remove files connected to the user's bookings.
        con.execute(
            "DELETE FROM booking_files "
            "WHERE booking_id IN ("
            "SELECT id FROM bookings WHERE student_id=? OR tutor_id=?"
            ") OR uploader_id=?",
            (user_id, user_id, user_id),
        )

        # Remove tutor recaps connected to the user or their bookings.
        con.execute(
            "DELETE FROM recaps "
            "WHERE booking_id IN ("
            "SELECT id FROM bookings WHERE student_id=? OR tutor_id=?"
            ") OR tutor_id=?",
            (user_id, user_id, user_id),
        )

        # Remove bookings.
        con.execute(
            "DELETE FROM bookings WHERE student_id=? OR tutor_id=?",
            (user_id, user_id),
        )

        # Remove profile information.
        con.execute(
            "DELETE FROM assessments WHERE student_id=?",
            (user_id,),
        )

        con.execute(
            "DELETE FROM availability WHERE tutor_id=?",
            (user_id,),
        )

        con.execute(
            "DELETE FROM student_profiles WHERE user_id=?",
            (user_id,),
        )

        con.execute(
            "DELETE FROM tutor_profiles WHERE user_id=?",
            (user_id,),
        )

        # Finally remove the login account itself.
        con.execute(
            "DELETE FROM users WHERE id=?",
            (user_id,),
        )

        con.commit()
        flash(f'{user["name"]} has been removed.', "success")

    except Exception:
        con.rollback()
        app.logger.exception("Account deletion failed")
        flash("The account could not be removed.", "error")

    finally:
        con.close()

    return redirect(url_for("admin_dashboard"))
@app.get("/files/<int:booking_id>/<int:file_id>")
@login_required()
def get_file(booking_id, file_id):
    con = db()
    f = con.execute(
        "SELECT bf.*,b.student_id,b.tutor_id FROM booking_files bf JOIN bookings b ON b.id=bf.booking_id WHERE bf.id=? AND bf.booking_id=?",
        (file_id, booking_id),
    ).fetchone()
    con.close()
    if not f: abort(404)
    if session["role"] != "admin" and session["user_id"] not in {f["student_id"], f["tutor_id"]}: abort(403)
    return send_from_directory(UPLOAD_DIR, f["stored_name"], as_attachment=True, download_name=f["filename"])


@app.errorhandler(413)
def too_large(e):
    return "File too large. Maximum upload is 12 MB.", 413


if __name__ == "__main__":
    app.run(debug=True)
