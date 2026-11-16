"""DJ Booking - Flask + SQLite.  Τρέξιμο:  pip install flask  &&  python app.py"""
import os, secrets, sqlite3
from datetime import date
from flask import Flask, g, request, session, redirect, url_for, flash, render_template, abort
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))
DB = os.environ.get("DB_PATH", "djbooking.db")
ST = {"pending": "Σε αναμονή", "accepted": "Αποδεκτό", "declined": "Απορρίφθηκε", "cancelled": "Ακυρώθηκε"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw TEXT, role TEXT, name TEXT, city TEXT);
CREATE TABLE IF NOT EXISTS djs(user_id INTEGER PRIMARY KEY, genres TEXT DEFAULT '', price REAL DEFAULT 0, bio TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, dj_id INT, shop_id INT, event TEXT, day TEXT,
  start INT, hours INT, msg TEXT, status TEXT DEFAULT 'pending', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, booking_id INT UNIQUE, dj_id INT, shop_id INT, rating INT, text TEXT);
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def q(sql, args=(), one=False):
    r = db().execute(sql, args).fetchall()
    return (r[0] if r else None) if one else r

def run(sql, args=()):
    c = db().execute(sql, args); db().commit(); return c

def me():
    return q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True) if "uid" in session else None

def need(role=None):
    u = me()
    if not u: abort(redirect(url_for("login")))
    if role and u["role"] != role: abort(403)
    return u

def tmin(s):  # "21:30" -> λεπτά
    h, m = s.split(":"); return int(h) * 60 + int(m)

def conflict(dj_id, day, start, hours, skip=0):
    """Υπάρχει ήδη αποδεκτή κράτηση που επικαλύπτεται;"""
    for b in q("SELECT * FROM bookings WHERE dj_id=? AND day=? AND status='accepted' AND id!=?", (dj_id, day, skip)):
        if start < b["start"] + b["hours"] * 60 and b["start"] < start + hours * 60:
            return True
    return False

@app.before_request
def csrf():
    if request.method == "POST":
        if request.form.get("_t") != session.get("_t"): abort(400)
    session.setdefault("_t", secrets.token_hex(16))

@app.context_processor
def ctx():
    return dict(user=me(), tok=session.get("_t"), ST=ST, hm=lambda m: f"{m // 60 % 24:02d}:{m % 60:02d}")

# ---------- Λογαριασμοί ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        if len(f["pw"]) < 8: flash("Ο κωδικός θέλει τουλάχιστον 8 χαρακτήρες."); return render_template("auth.html", reg=1)
        try:
            c = run("INSERT INTO users(email,pw,role,name,city) VALUES(?,?,?,?,?)",
                    (f["email"].lower().strip(), generate_password_hash(f["pw"]), f["role"] if f["role"] in ("dj", "shop") else "shop", f["name"].strip(), f["city"].strip()))
        except sqlite3.IntegrityError:
            flash("Αυτό το email υπάρχει ήδη."); return render_template("auth.html", reg=1)
        if f["role"] == "dj": run("INSERT INTO djs(user_id) VALUES(?)", (c.lastrowid,))
        session["uid"] = c.lastrowid
        return redirect(url_for("dashboard"))
    return render_template("auth.html", reg=1)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE email=?", (request.form["email"].lower().strip(),), one=True)
        if u and check_password_hash(u["pw"], request.form["pw"]):
            session["uid"] = u["id"]; return redirect(url_for("dashboard"))
        flash("Λάθος email ή κωδικός.")
    return render_template("auth.html", reg=0)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect(url_for("index"))

# ---------- Αναζήτηση DJ ----------
@app.route("/")
def index():
    a = request.args; sql = """SELECT u.id,u.name,u.city,d.genres,d.price,d.bio,
      (SELECT ROUND(AVG(rating),1) FROM reviews WHERE dj_id=u.id) rate,
      (SELECT COUNT(*) FROM reviews WHERE dj_id=u.id) n FROM users u JOIN djs d ON d.user_id=u.id WHERE 1=1"""
    args = []
    for key, col in (("city", "u.city"), ("genre", "d.genres"), ("name", "u.name")):
        if a.get(key): sql += f" AND {col} LIKE ?"; args.append(f"%{a[key]}%")
    if a.get("max"):
        try: sql += " AND d.price<=?"; args.append(float(a["max"]))
        except ValueError: pass
    rows = q(sql + " ORDER BY rate DESC, u.name", args)
    if a.get("day"):  # κρύψε όσους είναι πιασμένοι εκείνη τη μέρα
        busy = {r["dj_id"] for r in q("SELECT dj_id FROM bookings WHERE day=? AND status='accepted'", (a["day"],))}
        rows = [r for r in rows if r["id"] not in busy]
    return render_template("index.html", djs=rows, a=a)

@app.route("/dj/<int:i>")
def dj(i):
    d = q("SELECT u.*,d.genres,d.price,d.bio FROM users u JOIN djs d ON d.user_id=u.id WHERE u.id=?", (i,), one=True) or abort(404)
    revs = q("SELECT r.*,u.name shop FROM reviews r JOIN users u ON u.id=r.shop_id WHERE dj_id=? ORDER BY r.id DESC", (i,))
    return render_template("dj.html", d=d, revs=revs, today=date.today().isoformat())

# ---------- Κρατήσεις ----------
@app.route("/book/<int:i>", methods=["POST"])
def book(i):
    u = need("shop"); f = request.form
    d = q("SELECT price FROM djs WHERE user_id=?", (i,), one=True) or abort(404)
    try: hrs = max(1, min(12, int(f["hours"]))); st = tmin(f["start"])
    except (ValueError, KeyError): abort(400)
    if f["day"] < date.today().isoformat(): flash("Η ημερομηνία έχει περάσει."); return redirect(url_for("dj", i=i))
    if conflict(i, f["day"], st, hrs): flash("Ο DJ είναι ήδη κλεισμένος εκείνη την ώρα."); return redirect(url_for("dj", i=i))
    run("INSERT INTO bookings(dj_id,shop_id,event,day,start,hours,msg) VALUES(?,?,?,?,?,?,?)",
        (i, u["id"], f["event"][:80] or "Εκδήλωση", f["day"], st, hrs, f["msg"][:500]))
    flash("Το αίτημα στάλθηκε!"); return redirect(url_for("dashboard"))

@app.route("/booking/<int:i>/<act>", methods=["POST"])
def booking(i, act):
    u = need(); b = q("SELECT * FROM bookings WHERE id=?", (i,), one=True) or abort(404)
    if act in ("accepted", "declined") and u["id"] == b["dj_id"] and b["status"] == "pending":
        if act == "accepted" and conflict(b["dj_id"], b["day"], b["start"], b["hours"], b["id"]):
            flash("Επικάλυψη με άλλη αποδεκτή κράτηση."); return redirect(url_for("dashboard"))
        run("UPDATE bookings SET status=? WHERE id=?", (act, i))
    elif act == "cancelled" and u["id"] in (b["shop_id"], b["dj_id"]) and b["status"] in ("pending", "accepted"):
        run("UPDATE bookings SET status='cancelled' WHERE id=?", (i,))
    else: abort(403)
    return redirect(url_for("dashboard"))

@app.route("/review/<int:i>", methods=["POST"])
def review(i):
    u = need("shop"); b = q("SELECT * FROM bookings WHERE id=? AND shop_id=? AND status='accepted' AND day<=?", (i, u["id"], date.today().isoformat()), one=True) or abort(403)
    r = max(1, min(5, int(request.form["rating"])))
    try: run("INSERT INTO reviews(booking_id,dj_id,shop_id,rating,text) VALUES(?,?,?,?,?)", (i, b["dj_id"], u["id"], r, request.form["text"][:400]))
    except sqlite3.IntegrityError: flash("Έχεις ήδη αξιολογήσει.")
    return redirect(url_for("dj", i=b["dj_id"]))

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    u = need()
    if request.method == "POST" and u["role"] == "dj":
        f = request.form
        run("UPDATE djs SET genres=?,price=?,bio=? WHERE user_id=?", (f["genres"], float(f["price"] or 0), f["bio"][:600], u["id"]))
        run("UPDATE users SET city=? WHERE id=?", (f["city"], u["id"])); flash("Αποθηκεύτηκε ✔")
        return redirect(url_for("dashboard"))
    col, other = ("dj_id", "shop_id") if u["role"] == "dj" else ("shop_id", "dj_id")
    bs = q(f"SELECT b.*,x.name other,(SELECT price FROM djs WHERE user_id=b.dj_id)*b.hours total,"
           f"(SELECT 1 FROM reviews WHERE booking_id=b.id) rated FROM bookings b JOIN users x ON x.id=b.{other} WHERE b.{col}=? ORDER BY day DESC", (u["id"],))
    prof = q("SELECT * FROM djs WHERE user_id=?", (u["id"],), one=True)
    return render_template("dash.html", bs=bs, prof=prof, today=date.today().isoformat())

T = {
"base.html": """<!doctype html><html lang=el><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>DJ Booking</title><style>body{font-family:system-ui;max-width:800px;margin:auto;padding:14px;background:#12101c;color:#eee}
a{color:#a78bfa}input,select,textarea,button{font:inherit;padding:9px;margin:3px 0;border-radius:8px;border:1px solid #444;background:#1d1a2e;color:#eee;width:100%;box-sizing:border-box}
button{background:#7c3aed;border:0;font-weight:700;cursor:pointer}.c{background:#1d1a2e;padding:12px;border-radius:12px;margin:10px 0}
nav{display:flex;gap:12px;align-items:center;justify-content:space-between}nav form{width:auto}nav button{width:auto}.f{color:#facc15}</style>
<nav><b><a href="/">🎧 DJ Booking</a></b><span>{% if user %}<a href="/dashboard">{{user.name}}</a>
<form method=post action="/logout" style="display:inline"><input type=hidden name=_t value="{{tok}}"><button>Έξοδος</button></form>
{% else %}<a href="/login">Σύνδεση</a> · <a href="/register">Εγγραφή</a>{% endif %}</span></nav>
{% for m in get_flashed_messages() %}<p class=f>{{m}}</p>{% endfor %}{% block b %}{% endblock %}</html>""",
"auth.html": """{% extends "base.html" %}{% block b %}<form method=post class=c><input type=hidden name=_t value="{{tok}}">
<h2>{{'Εγγραφή' if reg else 'Σύνδεση'}}</h2>{% if reg %}<select name=role><option value=shop>Έχω μαγαζί</option><option value=dj>Είμαι DJ</option></select>
<input name=name placeholder="Όνομα μαγαζιού / DJ" required><input name=city placeholder="Πόλη">{% endif %}
<input name=email type=email placeholder=Email required><input name=pw type=password placeholder="Κωδικός (8+)" required><button>Συνέχεια</button></form>{% endblock %}""",
"index.html": """{% extends "base.html" %}{% block b %}<form class=c><input name=name placeholder=Όνομα value="{{a.name or ''}}"><input name=city placeholder=Πόλη value="{{a.city or ''}}">
<input name=genre placeholder="Στυλ μουσικής" value="{{a.genre or ''}}"><input name=max type=number placeholder="Μέγιστη τιμή €/ώρα" value="{{a.max or ''}}">
<input name=day type=date value="{{a.day or ''}}" title="Διαθέσιμοι εκείνη τη μέρα"><button>Αναζήτηση</button></form>
{% for d in djs %}<div class=c><a href="/dj/{{d.id}}"><b>{{d.name}}</b></a> · {{d.city}} · {{d.price|int}}€/ώρα
{% if d.n %}· <span class=f>★{{d.rate}}</span> ({{d.n}}){% endif %}<br>{{d.genres}}<br>{{d.bio}}</div>{% else %}<p>Δεν βρέθηκαν DJ.</p>{% endfor %}{% endblock %}""",
"dj.html": """{% extends "base.html" %}{% block b %}<div class=c><h2>{{d.name}}</h2>{{d.city}} · {{d.price|int}}€/ώρα<br>{{d.genres}}<p>{{d.bio}}</p></div>
{% if user and user.role=='shop' %}<form method=post action="/book/{{d.id}}" class=c><input type=hidden name=_t value="{{tok}}"><h3>Κράτηση</h3>
<input name=event placeholder="Είδος εκδήλωσης"><input name=day type=date min="{{today}}" required><input name=start type=time required>
<input name=hours type=number min=1 max=12 value=3><textarea name=msg placeholder="Μήνυμα"></textarea><button>Αποστολή αιτήματος</button></form>
{% elif not user %}<p><a href="/login">Συνδέσου</a> ως μαγαζί για να κλείσεις.</p>{% endif %}
<h3>Αξιολογήσεις</h3>{% for r in revs %}<div class=c><span class=f>{{'★'*r.rating}}</span> {{r.shop}}<br>{{r.text}}</div>{% else %}<p>Καμία ακόμα.</p>{% endfor %}{% endblock %}""",
"dash.html": """{% extends "base.html" %}{% block b %}
{% if user.role=='dj' %}<form method=post class=c><input type=hidden name=_t value="{{tok}}"><h3>Προφίλ DJ</h3><input name=city value="{{user.city or ''}}" placeholder=Πόλη>
<input name=genres value="{{prof.genres}}" placeholder="Στυλ μουσικής"><input name=price type=number value="{{prof.price|int}}" placeholder="€/ώρα"><textarea name=bio>{{prof.bio}}</textarea><button>Αποθήκευση</button></form>{% endif %}
<h3>Κρατήσεις</h3>{% for b in bs %}<div class=c><b>{{b.other}}</b> — {{b.event}}<br>{{b.day}} {{hm(b.start)}}–{{hm(b.start+b.hours*60)}} · {{b.total|int}}€ · {{ST[b.status]}}<br>{{b.msg}}
{% for act,lbl in ([('accepted','Αποδοχή'),('declined','Απόρριψη')] if user.role=='dj' and b.status=='pending' else []) + ([('cancelled','Ακύρωση')] if b.status in ('pending','accepted') else []) %}
<form method=post action="/booking/{{b.id}}/{{act}}" style="display:inline"><input type=hidden name=_t value="{{tok}}"><button style="width:auto">{{lbl}}</button></form>{% endfor %}
{% if user.role=='shop' and b.status=='accepted' and b.day<=today and not b.rated %}<form method=post action="/review/{{b.id}}"><input type=hidden name=_t value="{{tok}}">
<select name=rating><option>5</option><option>4</option><option>3</option><option>2</option><option>1</option></select><input name=text placeholder="Σχόλιο"><button>Αξιολόγηση</button></form>{% endif %}</div>
{% else %}<p>Καμία κράτηση ακόμα.</p>{% endfor %}{% endblock %}""",
}
app.jinja_loader = DictLoader(T)

with app.app_context():
    db().executescript(SCHEMA)

if __name__ == "__main__":
    app.run(debug=False, port=5000)
