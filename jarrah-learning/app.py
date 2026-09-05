import os, sqlite3, secrets, base64
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import requests

try:
    import stripe
except Exception:
    stripe = None

load_dotenv()
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "jarrah_learning.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=12 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/")

SCHEMA = """
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
 stripe_account_id TEXT, photo_path TEXT
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
 topic TEXT, strengths TEXT, weaknesses TEXT, status TEXT NOT NULL DEFAULT 'pending',
 paid INTEGER NOT NULL DEFAULT 0, price_cents INTEGER NOT NULL DEFAULT 2500,
 admin_fee_cents INTEGER NOT NULL DEFAULT 400, stripe_session_id TEXT, zoom_join_url TEXT,
 zoom_start_url TEXT, cancellation_fee_cents INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
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

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    con = db(); con.executescript(SCHEMA)
    email = os.getenv("ADMIN_EMAIL", "admin@example.com").lower()
    pw = os.getenv("ADMIN_PASSWORD", "ChangeThisPassword123!")
    if not con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        con.execute("INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",
                    ("admin","Administrator",email,generate_password_hash(pw),datetime.utcnow().isoformat()))
    con.commit(); con.close()

init_db()

def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", role=role or "student"))
            if role and session.get("role") != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco

def current_user():
    if not session.get("user_id"): return None
    con=db(); row=con.execute("SELECT * FROM users WHERE id=?",(session["user_id"],)).fetchone(); con.close(); return row

def safe_next(value):
    if not value: return None
    p=urlparse(value)
    return value if not p.netloc and not p.scheme else None

def score_match(student, tutor):
    score=50
    ss=(student["subject"] or "").lower(); ts=(tutor["subjects"] or "").lower()
    if ss and ss in ts: score += 25
    cl=(student["course_level"] or "").lower(); hl=(tutor["highest_level"] or "").lower()
    if cl and any(x in hl for x in ["ib","ap","cegep","college"] if x in cl): score += 8
    sf=(student["format_pref"] or "").lower(); tf=(tutor["format"] or "").lower()
    if sf in tf or tf == "either" or sf == "either": score += 10
    pref=(student["tutor_gender_pref"] or "").lower()
    if "no preference" in pref or not pref: score += 5
    return min(score,99)

def create_zoom_meeting(topic, start_at, duration=60):
    aid=os.getenv("ZOOM_ACCOUNT_ID"); cid=os.getenv("ZOOM_CLIENT_ID"); secret=os.getenv("ZOOM_CLIENT_SECRET")
    if not all([aid,cid,secret]): return None
    auth=base64.b64encode(f"{cid}:{secret}".encode()).decode()
    tok=requests.post("https://zoom.us/oauth/token", params={"grant_type":"account_credentials","account_id":aid}, headers={"Authorization":f"Basic {auth}"}, timeout=15)
    tok.raise_for_status(); access=tok.json()["access_token"]
    host=os.getenv("ZOOM_HOST_USER_ID","me")
    payload={"topic":topic,"type":2,"start_time":start_at,"duration":duration,"timezone":"America/Toronto","settings":{"waiting_room":True,"join_before_host":False}}
    r=requests.post(f"https://api.zoom.us/v2/users/{host}/meetings", json=payload, headers={"Authorization":f"Bearer {access}","Content-Type":"application/json"}, timeout=15)
    r.raise_for_status(); data=r.json(); return {"join_url":data.get("join_url"),"start_url":data.get("start_url")}

@app.get("/")
def home(): return render_template("home.html", user=current_user())

@app.route("/signup/student", methods=["GET","POST"])
def signup_student():
    if request.method=="POST":
        f=request.form; con=db()
        try:
            cur=con.execute("INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",("student",f["name"],f["email"].lower(),generate_password_hash(f["password"]),datetime.utcnow().isoformat()))
            uid=cur.lastrowid
            con.execute("INSERT INTO student_profiles VALUES(?,?,?,?,?,?,?,?,?)",(uid,f.get("grade"),f.get("subject"),f.get("course_level"),f.get("format_pref"),f.get("tutor_gender_pref"),f.get("goal"),f.get("target"),f.get("notes")))
            con.commit(); session.update(user_id=uid,role="student"); return redirect(url_for("student_dashboard"))
        except sqlite3.IntegrityError:
            flash("That email is already registered.","error")
        finally: con.close()
    return render_template("signup_student.html")

@app.route("/signup/tutor", methods=["GET","POST"])
def signup_tutor():
    if request.method=="POST":
        f=request.form; con=db()
        try:
            cur=con.execute("INSERT INTO users(role,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",("tutor",f["name"],f["email"].lower(),generate_password_hash(f["password"]),datetime.utcnow().isoformat()))
            uid=cur.lastrowid; photo=None
            up=request.files.get("photo")
            if up and up.filename:
                ext=Path(secure_filename(up.filename)).suffix.lower()
                if ext in {".jpg",".jpeg",".png",".webp"}:
                    stored=f"tutor_{uid}_{secrets.token_hex(6)}{ext}"; up.save(UPLOAD_DIR/stored); photo=stored
            con.execute("INSERT INTO tutor_profiles(user_id,age,subjects,highest_level,format,tutoring_type,qualifications,teaching_style,photo_path) VALUES(?,?,?,?,?,?,?,?,?)",(uid,f.get("age"),f.get("subjects"),f.get("highest_level"),f.get("format"),f.get("tutoring_type"),f.get("qualifications"),f.get("teaching_style"),photo))
            con.commit(); session.update(user_id=uid,role="tutor"); return redirect(url_for("tutor_dashboard"))
        except sqlite3.IntegrityError: flash("That email is already registered.","error")
        finally: con.close()
    return render_template("signup_tutor.html")

@app.route("/login/<role>", methods=["GET","POST"])
def login(role):
    if role not in {"student","tutor","admin"}: abort(404)
    if request.method=="POST":
        con=db(); u=con.execute("SELECT * FROM users WHERE email=? AND role=?",(request.form["email"].lower(),role)).fetchone(); con.close()
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear(); session.update(user_id=u["id"],role=u["role"])
            endpoint={"student":"student_dashboard","tutor":"tutor_dashboard","admin":"admin_dashboard"}[role]
            return redirect(safe_next(request.args.get("next")) or url_for(endpoint))
        flash("Incorrect email or password.","error")
    return render_template("login.html", role=role)

@app.get("/logout")
def logout(): session.clear(); return redirect(url_for("home"))

@app.get("/student")
@login_required("student")
def student_dashboard():
    uid=session["user_id"]; con=db()
    s=con.execute("SELECT u.name,u.email,p.* FROM users u JOIN student_profiles p ON p.user_id=u.id WHERE u.id=?",(uid,)).fetchone()
    tutors=con.execute("SELECT u.id,u.name,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE p.approved=1").fetchall()
    matches=sorted([(score_match(s,t),t) for t in tutors], key=lambda x:x[0], reverse=True)
    bookings=con.execute("SELECT b.*,u.name tutor_name FROM bookings b JOIN users u ON u.id=b.tutor_id WHERE b.student_id=? ORDER BY b.start_at DESC",(uid,)).fetchall()
    assessments=con.execute("SELECT * FROM assessments WHERE student_id=? ORDER BY assessment_date",(uid,)).fetchall()
    con.close(); return render_template("student.html", profile=s,matches=matches,bookings=bookings,assessments=assessments)

@app.post("/student/assessment")
@login_required("student")
def add_assessment():
    con=db(); con.execute("INSERT INTO assessments(student_id,title,assessment_date,notes) VALUES(?,?,?,?)",(session["user_id"],request.form["title"],request.form["assessment_date"],request.form.get("notes"))); con.commit(); con.close(); flash("Assessment added.","success"); return redirect(url_for("student_dashboard"))

@app.get("/student/tutor/<int:tutor_id>")
@login_required("student")
def tutor_detail(tutor_id):
    con=db(); tutor=con.execute("SELECT u.id,u.name,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE u.id=? AND p.approved=1",(tutor_id,)).fetchone()
    slots=con.execute("SELECT * FROM availability WHERE tutor_id=? AND booked=0 AND start_at>? ORDER BY start_at",(tutor_id,datetime.now().isoformat())).fetchall(); con.close()
    if not tutor: abort(404)
    return render_template("tutor_detail.html",tutor=tutor,slots=slots)

@app.route("/student/book/<int:slot_id>", methods=["GET","POST"])
@login_required("student")
def book(slot_id):
    con=db(); slot=con.execute("SELECT a.*,u.name tutor_name,p.tutoring_type,p.stripe_account_id FROM availability a JOIN users u ON u.id=a.tutor_id JOIN tutor_profiles p ON p.user_id=u.id WHERE a.id=? AND a.booked=0",(slot_id,)).fetchone()
    if not slot: con.close(); abort(404)
    if request.method=="POST":
        paid = "volunteer" not in (slot["tutoring_type"] or "").lower() or "paid" in request.form.get("session_type","paid")
        price=2500 if paid else 0
        cur=con.execute("INSERT INTO bookings(student_id,tutor_id,availability_id,start_at,end_at,topic,strengths,weaknesses,status,paid,price_cents,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (session["user_id"],slot["tutor_id"],slot_id,slot["start_at"],slot["end_at"],request.form.get("topic"),request.form.get("strengths"),request.form.get("weaknesses"),"pending_payment" if price else "confirmed",0,price,datetime.utcnow().isoformat()))
        bid=cur.lastrowid
        for up in request.files.getlist("files"):
            if up and up.filename:
                original=secure_filename(up.filename); ext=Path(original).suffix.lower()
                if ext in {".pdf",".jpg",".jpeg",".png",".webp",".docx"}:
                    stored=f"booking_{bid}_{secrets.token_hex(7)}{ext}"; up.save(UPLOAD_DIR/stored)
                    con.execute("INSERT INTO booking_files(booking_id,uploader_id,filename,stored_name,kind) VALUES(?,?,?,?,?)",(bid,session["user_id"],original,stored,"student_prep"))
        con.execute("UPDATE availability SET booked=1 WHERE id=?",(slot_id,)); con.commit()
        if price==0:
            try:
                z=create_zoom_meeting(f"Jarrah Learning tutoring: {slot['tutor_name']}",slot["start_at"])
                if z: con.execute("UPDATE bookings SET zoom_join_url=?,zoom_start_url=? WHERE id=?",(z["join_url"],z["start_url"],bid)); con.commit()
            except Exception as e: app.logger.warning("Zoom creation failed: %s",e)
            con.close(); flash("Session booked.","success"); return redirect(url_for("student_dashboard"))
        con.close(); return redirect(url_for("checkout_booking",booking_id=bid))
    con.close(); return render_template("book.html",slot=slot)

@app.get("/booking/<int:booking_id>/checkout")
@login_required("student")
def checkout_booking(booking_id):
    con=db(); b=con.execute("SELECT b.*,tp.stripe_account_id,u.name tutor_name FROM bookings b JOIN tutor_profiles tp ON tp.user_id=b.tutor_id JOIN users u ON u.id=b.tutor_id WHERE b.id=? AND b.student_id=?",(booking_id,session["user_id"])).fetchone(); con.close()
    if not b: abort(404)
    if b["price_cents"]==0: return redirect(url_for("student_dashboard"))
    key=os.getenv("STRIPE_SECRET_KEY")
    if not key or not stripe or not b["stripe_account_id"]:
        return render_template("payment_setup_needed.html", booking=b)
    stripe.api_key=key
    cs=stripe.checkout.Session.create(mode="payment",line_items=[{"price_data":{"currency":"cad","product_data":{"name":f"Tutoring with {b['tutor_name']}"},"unit_amount":b["price_cents"]},"quantity":1}],
        success_url=f"{BASE_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",cancel_url=f"{BASE_URL}/student",
        payment_intent_data={"application_fee_amount":b["admin_fee_cents"],"transfer_data":{"destination":b["stripe_account_id"]}},metadata={"booking_id":str(b["id"])})
    con=db(); con.execute("UPDATE bookings SET stripe_session_id=? WHERE id=?",(cs.id,b["id"])); con.commit(); con.close(); return redirect(cs.url,code=303)

@app.get("/payment/success")
@login_required("student")
def payment_success():
    flash("Payment submitted. Your booking will confirm when Stripe verifies it.","success"); return redirect(url_for("student_dashboard"))

@app.post("/stripe/webhook")
def stripe_webhook():
    secret=os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret or not stripe: return ("Stripe not configured",400)
    try: event=stripe.Webhook.construct_event(request.data,request.headers.get("Stripe-Signature"),secret)
    except Exception: return ("Invalid webhook",400)
    if event["type"]=="checkout.session.completed":
        obj=event["data"]["object"]; bid=int(obj.get("metadata",{}).get("booking_id",0) or 0)
        if bid:
            con=db(); b=con.execute("SELECT b.*,u.name tutor_name FROM bookings b JOIN users u ON u.id=b.tutor_id WHERE b.id=?",(bid,)).fetchone()
            if b and not b["paid"]:
                join=start=None
                try:
                    z=create_zoom_meeting(f"Jarrah Learning tutoring: {b['tutor_name']}",b["start_at"])
                    if z: join,start=z["join_url"],z["start_url"]
                except Exception as e: app.logger.warning("Zoom creation failed: %s",e)
                con.execute("UPDATE bookings SET paid=1,status='confirmed',zoom_join_url=COALESCE(?,zoom_join_url),zoom_start_url=COALESCE(?,zoom_start_url) WHERE id=?",(join,start,bid)); con.commit()
            con.close()
    return ("ok",200)

@app.post("/booking/<int:booking_id>/cancel")
@login_required()
def cancel_booking(booking_id):
    uid=session["user_id"]; role=session["role"]; con=db(); b=con.execute("SELECT * FROM bookings WHERE id=?",(booking_id,)).fetchone()
    if not b or (role=="student" and b["student_id"]!=uid) or (role=="tutor" and b["tutor_id"]!=uid): con.close(); abort(403)
    start=datetime.fromisoformat(b["start_at"]); late=(start-datetime.now()) < timedelta(hours=24)
    fee = b["price_cents"] if late and role=="student" and b["price_cents"] else 0
    con.execute("UPDATE bookings SET status='cancelled',cancellation_fee_cents=? WHERE id=?",(fee,booking_id)); con.execute("UPDATE availability SET booked=0 WHERE id=?",(b["availability_id"],)); con.commit(); con.close()
    flash("Booking cancelled." + (" A late-cancellation fee has been recorded." if fee else ""),"success"); return redirect(url_for(f"{role}_dashboard"))

@app.get("/tutor")
@login_required("tutor")
def tutor_dashboard():
    uid=session["user_id"]; con=db(); p=con.execute("SELECT u.*,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE u.id=?",(uid,)).fetchone()
    slots=con.execute("SELECT * FROM availability WHERE tutor_id=? ORDER BY start_at DESC LIMIT 30",(uid,)).fetchall()
    bookings=con.execute("SELECT b.*,u.name student_name FROM bookings b JOIN users u ON u.id=b.student_id WHERE b.tutor_id=? ORDER BY b.start_at DESC",(uid,)).fetchall(); con.close()
    return render_template("tutor.html",profile=p,slots=slots,bookings=bookings)

@app.post("/tutor/availability")
@login_required("tutor")
def tutor_availability():
    start=datetime.fromisoformat(request.form["start_at"]); end=start+timedelta(minutes=int(request.form.get("minutes",60)))
    con=db(); con.execute("INSERT INTO availability(tutor_id,start_at,end_at) VALUES(?,?,?)",(session["user_id"],start.isoformat(timespec="minutes"),end.isoformat(timespec="minutes"))); con.commit(); con.close(); flash("Availability added.","success"); return redirect(url_for("tutor_dashboard"))

@app.post("/tutor/booking/<int:booking_id>/recap")
@login_required("tutor")
def tutor_recap(booking_id):
    con=db(); b=con.execute("SELECT * FROM bookings WHERE id=? AND tutor_id=?",(booking_id,session["user_id"])).fetchone()
    if not b: con.close(); abort(404)
    con.execute("INSERT INTO recaps(booking_id,tutor_id,summary,homework,created_at) VALUES(?,?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET summary=excluded.summary,homework=excluded.homework,created_at=excluded.created_at",(booking_id,session["user_id"],request.form["summary"],request.form.get("homework"),datetime.utcnow().isoformat()))
    for up in request.files.getlist("files"):
        if up and up.filename:
            original=secure_filename(up.filename); ext=Path(original).suffix.lower()
            if ext in {".pdf",".jpg",".jpeg",".png",".webp",".docx"}:
                stored=f"recap_{booking_id}_{secrets.token_hex(7)}{ext}"; up.save(UPLOAD_DIR/stored); con.execute("INSERT INTO booking_files(booking_id,uploader_id,filename,stored_name,kind) VALUES(?,?,?,?,?)",(booking_id,session["user_id"],original,stored,"tutor_notes"))
    con.execute("UPDATE bookings SET status='completed' WHERE id=?",(booking_id,)); con.commit(); con.close(); flash("Recap saved and session completed.","success"); return redirect(url_for("tutor_dashboard"))

@app.get("/tutor/stripe/connect")
@login_required("tutor")
def tutor_stripe_connect():
    if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
        flash("Add STRIPE_SECRET_KEY first.","error"); return redirect(url_for("tutor_dashboard"))
    stripe.api_key=os.getenv("STRIPE_SECRET_KEY"); con=db(); p=con.execute("SELECT stripe_account_id FROM tutor_profiles WHERE user_id=?",(session["user_id"],)).fetchone(); acct=p["stripe_account_id"]
    if not acct:
        user=con.execute("SELECT email FROM users WHERE id=?",(session["user_id"],)).fetchone(); a=stripe.Account.create(type="express",country="CA",email=user["email"],capabilities={"transfers":{"requested":True}}); acct=a.id; con.execute("UPDATE tutor_profiles SET stripe_account_id=? WHERE user_id=?",(acct,session["user_id"])); con.commit()
    con.close(); link=stripe.AccountLink.create(account=acct,refresh_url=f"{BASE_URL}/tutor",return_url=f"{BASE_URL}/tutor",type="account_onboarding"); return redirect(link.url,code=303)

@app.get("/admin")
@login_required("admin")
def admin_dashboard():
    con=db(); students=con.execute("SELECT COUNT(*) n FROM users WHERE role='student'").fetchone()["n"]; tutors=con.execute("SELECT COUNT(*) n FROM users WHERE role='tutor'").fetchone()["n"]
    pending=con.execute("SELECT u.id,u.name,u.email,p.* FROM users u JOIN tutor_profiles p ON p.user_id=u.id WHERE p.approved=0").fetchall()
    bookings=con.execute("SELECT b.*,s.name student_name,t.name tutor_name FROM bookings b JOIN users s ON s.id=b.student_id JOIN users t ON t.id=b.tutor_id ORDER BY b.start_at DESC LIMIT 100").fetchall()
    revenue=con.execute("SELECT COALESCE(SUM(admin_fee_cents),0) cents FROM bookings WHERE paid=1").fetchone()["cents"]
    con.close(); return render_template("admin.html",students=students,tutors=tutors,pending=pending,bookings=bookings,revenue=revenue)

@app.post("/admin/tutor/<int:tutor_id>/approve")
@login_required("admin")
def approve_tutor(tutor_id):
    con=db(); con.execute("UPDATE tutor_profiles SET approved=1 WHERE user_id=?",(tutor_id,)); con.commit(); con.close(); flash("Tutor approved.","success"); return redirect(url_for("admin_dashboard"))

@app.get("/files/<int:booking_id>/<int:file_id>")
@login_required()
def get_file(booking_id,file_id):
    con=db(); f=con.execute("SELECT bf.*,b.student_id,b.tutor_id FROM booking_files bf JOIN bookings b ON b.id=bf.booking_id WHERE bf.id=? AND bf.booking_id=?",(file_id,booking_id)).fetchone(); con.close()
    if not f: abort(404)
    if session["role"]!="admin" and session["user_id"] not in {f["student_id"],f["tutor_id"]}: abort(403)
    return send_from_directory(UPLOAD_DIR,f["stored_name"],as_attachment=True,download_name=f["filename"])

@app.errorhandler(413)
def too_large(e): return "File too large. Maximum upload is 12 MB.",413

if __name__ == "__main__":
    app.run(debug=True)
