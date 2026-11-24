"""Login, roles and user management for the Vinex stock app.

Roles
    admin   - everything, including products, locations, imports and users
    staff   - view everything and book stock movements (receive / issue / transfer / stocktake)
    viewer  - read-only

Every /api route except /api/login requires a signed-in user. The username of the
signed-in user is stored on each movement (it is not typed in by the operator).

Environment variables
    VINEX_ADMIN_USER / VINEX_ADMIN_PASSWORD  first admin account (created only when there are no users).
                                             If no password is given, a random one is printed in the log once.
    VINEX_ADMIN_RESET=1                      reset that admin's password to VINEX_ADMIN_PASSWORD at start-up
                                             (lock-out recovery - remove the variable afterwards)
    VINEX_SECRET_KEY                         signs session cookies (generated and stored in the database if absent)
    VINEX_COOKIE_SECURE=1                    send the cookie over HTTPS only (automatic on Railway)
    VINEX_BEHIND_PROXY=1                     trust X-Forwarded-* headers (automatic on Railway)
"""
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import timedelta

from flask import g, jsonify, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("admin", "staff", "viewer")
MIN_PASSWORD, MAX_PASSWORD = 8, 128
MAX_FAILS, LOCK_SECONDS = 5, 300
SESSION_DAYS = 7
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
ON_RAILWAY = any(k.startswith("RAILWAY_") for k in os.environ)

_fails = {}  # (ip, username) -> [failed count, locked until]
_fails_lock = threading.Lock()
_DUMMY_HASH = generate_password_hash("not-a-real-password")  # so unknown users cost the same time as real ones


def user_dict(u):
    return {"id": u["id"], "username": u["username"], "role": u["role"], "active": bool(u["active"]), "created_at": u["created_at"]}


def check_new_password(pw, ApiError):
    if not isinstance(pw, str) or not (MIN_PASSWORD <= len(pw) <= MAX_PASSWORD):
        raise ApiError(f"Password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters")


def allowed(role, method, path):
    if role == "admin":
        return True
    if path == "/api/logout" or path == "/api/me/password":
        return True
    if path.startswith("/api/users"):
        return False
    if method in ("GET", "HEAD"):
        return True
    return role == "staff" and method == "POST" and path == "/api/movements"


def _secret_key(connect):
    env = os.environ.get("VINEX_SECRET_KEY", "").strip()
    if env:
        return env
    db = connect()
    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('secret_key', ?)", (secrets.token_hex(32),))
    key = db.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()["value"]
    db.close()
    return key


def _bootstrap_admin(connect):
    db = connect()
    name = os.environ.get("VINEX_ADMIN_USER", "admin").strip() or "admin"
    pwd = os.environ.get("VINEX_ADMIN_PASSWORD", "")
    if pwd and not (MIN_PASSWORD <= len(pwd) <= MAX_PASSWORD):
        print(f"[auth] VINEX_ADMIN_PASSWORD must be {MIN_PASSWORD}-{MAX_PASSWORD} characters - ignoring it.", flush=True)
        pwd = ""
    if not USERNAME_RE.match(name):
        name = "admin"
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if users == 0:
        generated = not pwd
        pwd = pwd or secrets.token_urlsafe(9)
        db.execute("INSERT INTO users(username, password_hash, role) VALUES (?,?, 'admin')", (name, generate_password_hash(pwd)))
        if generated:
            print("\n" + "=" * 60 + f"\n  FIRST ADMIN ACCOUNT CREATED\n  username: {name}\n  password: {pwd}\n"
                  "  Sign in and change this password now.\n" + "=" * 60 + "\n", flush=True)
        else:
            print(f"[auth] Admin account '{name}' created from VINEX_ADMIN_PASSWORD.", flush=True)
    elif pwd and os.environ.get("VINEX_ADMIN_RESET") == "1":
        row = db.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()
        if row:
            db.execute("UPDATE users SET password_hash=?, role='admin', active=1 WHERE id=?", (generate_password_hash(pwd), row["id"]))
        else:
            db.execute("INSERT INTO users(username, password_hash, role) VALUES (?,?, 'admin')", (name, generate_password_hash(pwd)))
        print(f"[auth] Password for admin '{name}' was reset. Remove VINEX_ADMIN_RESET now.", flush=True)
    db.close()


def init(app, get_db, connect, ApiError):
    app.secret_key = _secret_key(connect)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("VINEX_COOKIE_SECURE") == "1" or ON_RAILWAY,
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )
    if ON_RAILWAY or os.environ.get("VINEX_BEHIND_PROXY") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    _bootstrap_admin(connect)

    def current_user(db):
        uid = session.get("uid")
        if not uid:
            return None
        u = db.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        # the cookie carries a fingerprint of the password hash, so changing/resetting a password signs old sessions out
        if not u or session.get("ph") != u["password_hash"][-16:]:
            session.clear()
            return None
        return u

    @app.before_request
    def guard():
        path = request.path
        if not path.startswith("/api/"):
            return None
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.is_json:
            return jsonify(error="JSON body required"), 415  # also blocks cross-site form posts
        if path == "/api/login":
            return None
        user = current_user(get_db())
        if not user:
            return jsonify(error="Please sign in"), 401
        if not allowed(user["role"], request.method, path):
            return jsonify(error="You don't have permission to do that"), 403
        g.user = user
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ sign in / out
    @app.post("/api/login")
    def login():
        d = request.get_json(silent=True) or {}
        username, password = str(d.get("username", "")).strip(), str(d.get("password", ""))
        key, now = (request.remote_addr, username.lower()), time.time()
        with _fails_lock:
            rec = _fails.get(key)
            if rec and rec[1] > now:
                raise ApiError(f"Too many failed attempts. Try again in {int(rec[1] - now) // 60 + 1} min.", 429)
        u = get_db().execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        ok = check_password_hash(u["password_hash"] if u else _DUMMY_HASH, password) and u is not None
        if not ok:
            with _fails_lock:
                if len(_fails) > 5000:
                    _fails.clear()
                count = (_fails.get(key) or [0, 0])[0] + 1
                _fails[key] = [0, now + LOCK_SECONDS] if count >= MAX_FAILS else [count, 0]
            raise ApiError("Wrong username or password", 401)
        with _fails_lock:
            _fails.pop(key, None)
        session.clear()
        session.permanent = True
        session["uid"], session["ph"] = u["id"], u["password_hash"][-16:]
        return jsonify(user_dict(u))

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    def me():
        return jsonify(user_dict(g.user))

    @app.post("/api/me/password")
    def change_own_password():
        d = request.get_json(force=True) or {}
        if not check_password_hash(g.user["password_hash"], str(d.get("current", ""))):
            raise ApiError("Current password is wrong")
        check_new_password(d.get("new"), ApiError)
        new_hash = generate_password_hash(d["new"])
        get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, g.user["id"]))
        session["ph"] = new_hash[-16:]
        return jsonify(ok=True)

    # ------------------------------------------------------------ user management (admin only)
    @app.get("/api/users")
    def list_users():
        return jsonify([user_dict(u) for u in get_db().execute("SELECT * FROM users ORDER BY username")])

    @app.post("/api/users")
    def create_user():
        d = request.get_json(force=True) or {}
        username, role = str(d.get("username", "")).strip(), d.get("role", "staff")
        if not USERNAME_RE.match(username):
            raise ApiError("Username: 3-32 letters, digits, dot, dash or underscore")
        if role not in ROLES:
            raise ApiError("Unknown role")
        check_new_password(d.get("password"), ApiError)
        try:
            cur = get_db().execute("INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
                                   (username, generate_password_hash(d["password"]), role))
        except sqlite3.IntegrityError:
            raise ApiError(f"User '{username}' already exists", 409)
        return jsonify(id=cur.lastrowid), 201

    @app.put("/api/users/<int:uid>")
    def update_user(uid):
        db, d = get_db(), request.get_json(force=True) or {}
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise ApiError("User not found", 404)
        role = d.get("role", u["role"])
        active = 1 if d.get("active", bool(u["active"])) else 0
        if role not in ROLES:
            raise ApiError("Unknown role")
        new_hash = u["password_hash"]
        if d.get("password"):
            check_new_password(d["password"], ApiError)
            new_hash = generate_password_hash(d["password"])
        if u["role"] == "admin" and u["active"] and (role != "admin" or not active):
            others = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id!=?", (uid,)).fetchone()[0]
            if others == 0:
                raise ApiError("There must be at least one active admin")
        db.execute("UPDATE users SET role=?, active=?, password_hash=? WHERE id=?", (role, active, new_hash, uid))
        if uid == g.user["id"] and new_hash != u["password_hash"]:
            session["ph"] = new_hash[-16:]
        return jsonify(ok=True)
