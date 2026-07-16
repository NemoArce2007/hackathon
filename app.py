import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for


def load_local_env(path=".env.local"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"')


load_local_env()

DATABASE = os.environ.get("DATABASE_PATH", "scoring.db")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_REDIRECT_URI = os.environ.get("FEISHU_REDIRECT_URI", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
ALLOWED_TENANT_KEY = os.environ.get("FEISHU_TENANT_KEY", "")
ADMIN_OPEN_IDS = {item.strip() for item in os.environ.get("ADMIN_OPEN_IDS", "").split(",") if item.strip()}
ALLOW_DEV_LOGIN = os.environ.get("ALLOW_DEV_LOGIN", "1") == "1"
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "5000"))

FEISHU_AUTHORIZE_URL = "https://open.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_CONTACT_SEARCH_URL = "https://open.feishu.cn/open-apis/contact/v3/users/search"
FEISHU_LOGIN_SCOPE = "auth:user.id:read contact:user:search offline_access"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)


def feishu_redirect_uri():
    if FEISHU_REDIRECT_URI:
        return FEISHU_REDIRECT_URI
    callback_path = url_for("feishu_callback")
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{callback_path}"
    return url_for("feishu_callback", _external=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def set_default(key, value):
    get_db().execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


def ensure_columns(table_name, columns):
    existing = {row["name"] for row in get_db().execute(f"PRAGMA table_info({table_name})").fetchall()}
    for column_name, definition in columns.items():
        if column_name not in existing:
            get_db().execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feishu_open_id TEXT NOT NULL UNIQUE,
        feishu_user_id TEXT,
        name TEXT NOT NULL,
        avatar_url TEXT,
        tenant_key TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feishu_open_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        order_index INTEGER NOT NULL UNIQUE,
        captain_open_id TEXT NOT NULL UNIQUE,
        captain_name TEXT NOT NULL,
        demand_vote INTEGER NOT NULL DEFAULT 0 CHECK (demand_vote IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS judges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        order_index INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS peer_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voter_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        target_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        vote INTEGER NOT NULL CHECK (vote IN (0, 1)),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (voter_team_id, target_team_id)
    );
    CREATE TABLE IF NOT EXISTS judge_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        judge_id INTEGER NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
        target_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        vote INTEGER NOT NULL CHECK (vote IN (0, 1)),
        entered_by_admin_open_id TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (judge_id, target_team_id)
    );
    CREATE TABLE IF NOT EXISTS tie_break_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_no INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        closed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS tie_break_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES tie_break_rounds(id) ON DELETE CASCADE,
        team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        UNIQUE (round_id, team_id)
    );
    CREATE TABLE IF NOT EXISTS tie_break_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES tie_break_rounds(id) ON DELETE CASCADE,
        team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        voter_open_id TEXT NOT NULL,
        voter_name TEXT NOT NULL,
        tenant_key TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (round_id, voter_open_id)
    );
    CREATE TABLE IF NOT EXISTS tie_break_blacklist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        open_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    ensure_columns(
        "users",
        {
            "feishu_access_token": "TEXT",
            "feishu_refresh_token": "TEXT",
            "token_expires_at": "TEXT",
            "refresh_expires_at": "TEXT",
            "token_scope": "TEXT",
        },
    )
    set_default("peer_m", "1")
    set_default("peer_status", "not_started")
    set_default("champion_team_id", "")
    set_default("champion_confirmed_at", "")
    set_default("event_countdown_target", "2026-07-17T09:00")
    set_default("event_countdown_title", "距离 AI 黑客松开始")
    if ADMIN_OPEN_IDS:
        for open_id in ADMIN_OPEN_IDS:
            db.execute("INSERT OR IGNORE INTO admins (feishu_open_id, name) VALUES (?, ?)", (open_id, "管理员"))
    db.commit()


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    get_db().commit()


def current_user():
    open_id = session.get("open_id")
    if not open_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE feishu_open_id = ?", (open_id,)).fetchone()


def expires_at(seconds):
    if not seconds:
        return ""
    return (datetime.now() + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


def token_expired(expires_at_value, skew_seconds=60):
    if not expires_at_value:
        return True
    try:
        expires_on = datetime.fromisoformat(expires_at_value)
    except ValueError:
        return True
    return expires_on <= datetime.now() + timedelta(seconds=skew_seconds)


def is_admin(user=None):
    user = user or current_user()
    if not user:
        return False
    row = get_db().execute("SELECT 1 FROM admins WHERE feishu_open_id = ?", (user["feishu_open_id"],)).fetchone()
    return row is not None


@app.before_request
def load_user():
    g.user = current_user()
    g.is_admin = is_admin(g.user)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not g.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def update_user_tokens(open_id, token_data):
    get_db().execute(
        """
        UPDATE users
        SET feishu_access_token = ?, feishu_refresh_token = ?, token_expires_at = ?, refresh_expires_at = ?, token_scope = ?, last_seen_at = CURRENT_TIMESTAMP
        WHERE feishu_open_id = ?
        """,
        (
            token_data.get("access_token", ""),
            token_data.get("refresh_token", ""),
            expires_at(token_data.get("expires_in")),
            expires_at(token_data.get("refresh_expires_in")),
            token_data.get("scope", ""),
            open_id,
        ),
    )
    get_db().commit()


def upsert_user(profile, token_data=None):
    tenant_key = profile.get("tenant_key") or profile.get("tenant", {}).get("tenant_key") or ""
    if ALLOWED_TENANT_KEY and tenant_key and tenant_key != ALLOWED_TENANT_KEY:
        abort(403)
    open_id = profile["open_id"]
    name = profile.get("name") or profile.get("en_name") or "飞书用户"
    if token_data:
        get_db().execute(
            """
            INSERT INTO users (
                feishu_open_id, feishu_user_id, name, avatar_url, tenant_key,
                feishu_access_token, feishu_refresh_token, token_expires_at, refresh_expires_at, token_scope
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(feishu_open_id) DO UPDATE SET
                feishu_user_id = excluded.feishu_user_id,
                name = excluded.name,
                avatar_url = excluded.avatar_url,
                tenant_key = excluded.tenant_key,
                feishu_access_token = excluded.feishu_access_token,
                feishu_refresh_token = excluded.feishu_refresh_token,
                token_expires_at = excluded.token_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                token_scope = excluded.token_scope,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (
                open_id,
                profile.get("user_id"),
                name,
                profile.get("avatar_url"),
                tenant_key,
                token_data.get("access_token", ""),
                token_data.get("refresh_token", ""),
                expires_at(token_data.get("expires_in")),
                expires_at(token_data.get("refresh_expires_in")),
                token_data.get("scope", ""),
            ),
        )
    else:
        get_db().execute(
            """
            INSERT INTO users (feishu_open_id, feishu_user_id, name, avatar_url, tenant_key)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(feishu_open_id) DO UPDATE SET
                feishu_user_id = excluded.feishu_user_id,
                name = excluded.name,
                avatar_url = excluded.avatar_url,
                tenant_key = excluded.tenant_key,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (open_id, profile.get("user_id"), name, profile.get("avatar_url"), tenant_key),
        )
    get_db().commit()
    session["open_id"] = open_id


def exchange_feishu_token(payload):
    token_resp = requests.post(
        FEISHU_TOKEN_URL,
        json=payload,
        timeout=15,
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()
    token_payload = token_data.get("data", token_data)
    access_token = token_payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"飞书未返回 access_token: {token_data}")
    return token_payload


def fetch_feishu_user_profile(access_token):
    user_resp = requests.get(FEISHU_USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    user_resp.raise_for_status()
    user_data = user_resp.json()
    return user_data.get("data", user_data)


def fetch_feishu_user(code):
    redirect_uri = feishu_redirect_uri()
    token_data = exchange_feishu_token(
        {
            "grant_type": "authorization_code",
            "client_id": FEISHU_APP_ID,
            "client_secret": FEISHU_APP_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    )
    profile = fetch_feishu_user_profile(token_data["access_token"])
    return profile, token_data


def ordered_teams():
    return get_db().execute("SELECT * FROM teams ORDER BY order_index, id").fetchall()


def list_judges():
    return get_db().execute("SELECT * FROM judges ORDER BY order_index, id").fetchall()


def get_team(team_id):
    return get_db().execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()


def get_judge(judge_id):
    return get_db().execute("SELECT * FROM judges WHERE id = ?", (judge_id,)).fetchone()


def get_captain_team(open_id):
    return get_db().execute("SELECT * FROM teams WHERE captain_open_id = ?", (open_id,)).fetchone()


def refresh_feishu_user_access_token(refresh_token):
    return exchange_feishu_token(
        {
            "grant_type": "refresh_token",
            "client_id": FEISHU_APP_ID,
            "client_secret": FEISHU_APP_SECRET,
            "refresh_token": refresh_token,
        }
    )


def get_user_access_token(user):
    access_token = user["feishu_access_token"] if "feishu_access_token" in user.keys() else ""
    if access_token and not token_expired(user["token_expires_at"]):
        return access_token
    refresh_token = user["feishu_refresh_token"] if "feishu_refresh_token" in user.keys() else ""
    if not refresh_token:
        raise RuntimeError("当前账号还没有飞书通讯录授权，请退出后重新使用飞书登录一次。")
    if user["refresh_expires_at"] and token_expired(user["refresh_expires_at"], skew_seconds=300):
        raise RuntimeError("飞书通讯录授权已过期，请退出后重新使用飞书登录一次。")
    try:
        token_data = refresh_feishu_user_access_token(refresh_token)
    except requests.HTTPError as exc:
        raise RuntimeError("刷新飞书登录态失败，请退出后重新使用飞书登录一次。") from exc
    update_user_tokens(user["feishu_open_id"], token_data)
    return token_data["access_token"]


def search_feishu_users(user, query="", limit=12):
    query = (query or "").strip()
    if not query:
        return []
    access_token = get_user_access_token(user)
    response = requests.post(
        FEISHU_CONTACT_SEARCH_URL,
        params={"page_size": min(limit, 20)},
        headers={"Authorization": f"Bearer {access_token}"},
        json={"query": query},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code", 0) != 0:
        raise RuntimeError(payload.get("msg") or "飞书通讯录搜索失败。")
    users = []
    for item in payload.get("data", {}).get("items", []):
        open_id = item.get("open_id") or item.get("id")
        if not open_id:
            continue
        meta = item.get("meta_data") or {}
        i18n = meta.get("i18n_names") or {}
        display_info = item.get("display_info") or ""
        display_text = re.sub(r"</?h>", "", display_info).strip()
        display_lines = [line.strip() for line in display_text.split("\n") if line.strip()]
        name = (
            item.get("localized_name")
            or item.get("name")
            or i18n.get("zh_cn")
            or (display_lines[0] if display_lines else "")
            or item.get("en_name")
            or "未命名用户"
        )
        department = item.get("department") or (display_lines[1] if len(display_lines) > 1 else "")
        enterprise_email = item.get("enterprise_email") or meta.get("enterprise_mail_address") or ""
        email = item.get("email") or meta.get("mail_address") or ""
        is_cross_tenant = item.get("is_cross_tenant", meta.get("is_cross_tenant", False))
        users.append(
            {
                "feishu_open_id": open_id,
                "name": name,
                "department": department,
                "email": email,
                "enterprise_email": enterprise_email,
                "is_cross_tenant": is_cross_tenant,
            }
        )
    return users


def peer_targets(team_id):
    teams = ordered_teams()
    if not teams:
        return []
    m = int(get_setting("peer_m", "1") or 1)
    index = next((idx for idx, team in enumerate(teams) if team["id"] == team_id), None)
    if index is None:
        return []
    return [teams[(index + offset) % len(teams)] for offset in range(1, m + 1)]


def validate_peer_m(value):
    teams_count = len(ordered_teams())
    if teams_count < 3:
        raise ValueError("至少需要 3 支队伍才能开启互评。")
    m = int(value)
    if m < 1 or not (m < teams_count / 2):
        raise ValueError(f"M 必须满足 1 <= M < N / 2；当前 N={teams_count}。")
    return m


def peer_vote_lookup():
    return {
        (row["voter_team_id"], row["target_team_id"]): row["vote"]
        for row in get_db().execute("SELECT voter_team_id, target_team_id, vote FROM peer_votes").fetchall()
    }


def peer_assignment_rows():
    lookup = peer_vote_lookup()
    rows = []
    for team in ordered_teams():
        targets = peer_targets(team["id"])
        rows.append(
            {
                "team": team,
                "targets": [{"team": target, "vote": lookup.get((team["id"], target["id"]))} for target in targets],
                "submitted": sum(1 for target in targets if lookup.get((team["id"], target["id"])) is not None),
            }
        )
    return rows


def score_rows():
    rows = []
    for team in ordered_teams():
        peer_count = get_db().execute(
            "SELECT COUNT(*) AS count FROM peer_votes WHERE target_team_id = ? AND vote = 1", (team["id"],)
        ).fetchone()["count"]
        judge_approvals = get_db().execute(
            "SELECT COUNT(*) AS count FROM judge_votes WHERE target_team_id = ? AND vote = 1", (team["id"],)
        ).fetchone()["count"]
        judge_score = judge_approvals * 2
        total = team["demand_vote"] + peer_count + judge_score
        rows.append({"team": team, "demand_vote": team["demand_vote"], "peer_votes": peer_count, "judge_approvals": judge_approvals, "judge_score": judge_score, "total": total})
    return sorted(rows, key=lambda row: (-row["total"], row["team"]["order_index"]))


def top_tied_teams():
    rows = score_rows()
    if not rows:
        return []
    top_score = rows[0]["total"]
    return [row["team"] for row in rows if row["total"] == top_score]


def tie_blacklist():
    return get_db().execute("SELECT * FROM tie_break_blacklist ORDER BY created_at DESC, id DESC").fetchall()


def is_blacklisted(open_id):
    row = get_db().execute("SELECT 1 FROM tie_break_blacklist WHERE open_id = ?", (open_id,)).fetchone()
    return row is not None


def active_round():
    return get_db().execute("SELECT * FROM tie_break_rounds WHERE status = 'open' ORDER BY round_no DESC LIMIT 1").fetchone()


def latest_closed_round():
    return get_db().execute("SELECT * FROM tie_break_rounds WHERE status = 'closed' ORDER BY round_no DESC LIMIT 1").fetchone()


def round_candidates(round_id):
    return get_db().execute(
        """
        SELECT t.* FROM tie_break_candidates c
        JOIN teams t ON t.id = c.team_id
        WHERE c.round_id = ?
        ORDER BY t.order_index
        """,
        (round_id,),
    ).fetchall()


def tie_results(round_id):
    return get_db().execute(
        """
        SELECT t.id, t.name, t.order_index, COUNT(v.id) AS votes
        FROM tie_break_candidates c
        JOIN teams t ON t.id = c.team_id
        LEFT JOIN tie_break_votes v ON v.round_id = c.round_id AND v.team_id = t.id
        WHERE c.round_id = ?
        GROUP BY t.id
        ORDER BY votes DESC, t.order_index
        """,
        (round_id,),
    ).fetchall()


def tie_top_results(round_id):
    results = tie_results(round_id)
    if not results:
        return []
    top_votes = results[0]["votes"]
    return [row for row in results if row["votes"] == top_votes]


def open_tie_round(team_ids):
    unique_ids = []
    for team_id in team_ids:
        int_team_id = int(team_id)
        if int_team_id not in unique_ids:
            unique_ids.append(int_team_id)
    if len(unique_ids) < 2:
        raise ValueError("候选队伍少于 2 支，不需要开启加赛。")
    row = get_db().execute("SELECT COALESCE(MAX(round_no), 0) + 1 AS next_no FROM tie_break_rounds").fetchone()
    cursor = get_db().execute("INSERT INTO tie_break_rounds (round_no, status) VALUES (?, 'open')", (row["next_no"],))
    round_id = cursor.lastrowid
    for team_id in unique_ids:
        get_db().execute("INSERT INTO tie_break_candidates (round_id, team_id) VALUES (?, ?)", (round_id, team_id))
    set_setting("champion_team_id", "")
    set_setting("champion_confirmed_at", "")
    return round_id


def leaderboard_state():
    rows = score_rows()
    top_teams = top_tied_teams()
    champion_id = get_setting("champion_team_id", "")
    return {
        "rows": rows,
        "top_tied_teams": top_teams,
        "top_tied_ids": [team["id"] for team in top_teams],
        "needs_tie_break": len(top_teams) > 1 and not active_round() and not champion_id,
        "champion_id": champion_id,
        "champion_confirmed_at": get_setting("champion_confirmed_at", ""),
    }


@app.route("/")
def index():
    return redirect(url_for("leaderboard"))


@app.route("/login")
def login():
    return render_template("login.html", feishu_ready=bool(FEISHU_APP_ID and FEISHU_APP_SECRET), allow_dev=ALLOW_DEV_LOGIN)


@app.route("/auth/feishu/start")
def feishu_start():
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        flash("尚未配置飞书应用。")
        return redirect(url_for("login"))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    query = urlencode({"app_id": FEISHU_APP_ID, "redirect_uri": feishu_redirect_uri(), "state": state, "scope": FEISHU_LOGIN_SCOPE})
    return redirect(f"{FEISHU_AUTHORIZE_URL}?{query}")


@app.route("/auth/feishu/callback")
def feishu_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400)
    code = request.args.get("code")
    if not code:
        abort(400)
    profile, token_data = fetch_feishu_user(code)
    upsert_user(profile, token_data=token_data)
    return redirect(url_for("leaderboard"))


@app.route("/auth/dev", methods=["POST"])
def dev_login():
    if not ALLOW_DEV_LOGIN:
        abort(404)
    open_id = request.form["open_id"].strip()
    name = request.form["name"].strip() or "本地用户"
    upsert_user({"open_id": open_id, "name": name, "tenant_key": ALLOWED_TENANT_KEY or "dev"})
    if request.form.get("admin") == "1":
        get_db().execute("INSERT OR IGNORE INTO admins (feishu_open_id, name) VALUES (?, ?)", (open_id, name))
        get_db().commit()
    return redirect(url_for("leaderboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("leaderboard"))


@app.route("/leaderboard")
def leaderboard():
    state = leaderboard_state()
    return render_template(
        "leaderboard.html",
        rows=state["rows"],
        active_round=active_round(),
        champion_id=state["champion_id"],
        champion_confirmed_at=state["champion_confirmed_at"],
        top_tied_teams=state["top_tied_teams"],
        top_tied_ids=state["top_tied_ids"],
        needs_tie_break=state["needs_tie_break"],
        refreshed_at=datetime.now().strftime("%H:%M:%S"),
    )


@app.route("/timer")
def timer():
    return render_template("timer.html")


@app.route("/countdown")
def countdown():
    return render_template(
        "countdown.html",
        target=get_setting("event_countdown_target", "2026-07-17T09:00"),
        countdown_title=get_setting("event_countdown_title", "距离 AI 黑客松开始"),
    )


@app.route("/peer", methods=["GET", "POST"])
@login_required
def peer():
    team = get_captain_team(g.user["feishu_open_id"])
    status = get_setting("peer_status", "not_started")
    peer_m = int(get_setting("peer_m", "1") or 1)
    if not team:
        return render_template("peer.html", team=None, targets=[], votes={}, status=status, peer_m=peer_m)
    targets = peer_targets(team["id"])
    if request.method == "POST":
        if status != "open":
            abort(403)
        for target in targets:
            vote = 1 if request.form.get(f"vote_{target['id']}") == "1" else 0
            get_db().execute(
                """
                INSERT INTO peer_votes (voter_team_id, target_team_id, vote)
                VALUES (?, ?, ?)
                ON CONFLICT(voter_team_id, target_team_id) DO UPDATE SET vote = excluded.vote, updated_at = CURRENT_TIMESTAMP
                """,
                (team["id"], target["id"], vote),
            )
        get_db().commit()
        return redirect(url_for("peer", saved=1))
    votes = {row["target_team_id"]: row["vote"] for row in get_db().execute("SELECT target_team_id, vote FROM peer_votes WHERE voter_team_id = ?", (team["id"],)).fetchall()}
    saved = request.args.get("saved") == "1"
    return render_template("peer.html", team=team, targets=targets, votes=votes, status=status, saved=saved, peer_m=peer_m)


@app.route("/tie-vote", methods=["GET", "POST"])
@login_required
def tie_vote():
    active = active_round()
    blocked = is_blacklisted(g.user["feishu_open_id"])
    if not active:
        return render_template("tie_vote.html", active_round=None, candidates=[], already=None, blocked=blocked)
    already = get_db().execute("SELECT * FROM tie_break_votes WHERE round_id = ? AND voter_open_id = ?", (active["id"], g.user["feishu_open_id"])).fetchone()
    candidates = round_candidates(active["id"])
    candidate_ids = {team["id"] for team in candidates}
    if request.method == "POST":
        if blocked:
            abort(403)
        if already:
            abort(403)
        team_id = int(request.form["team_id"])
        if team_id not in candidate_ids:
            abort(400)
        get_db().execute(
            "INSERT INTO tie_break_votes (round_id, team_id, voter_open_id, voter_name, tenant_key) VALUES (?, ?, ?, ?, ?)",
            (active["id"], team_id, g.user["feishu_open_id"], g.user["name"], g.user["tenant_key"]),
        )
        get_db().commit()
        return redirect(url_for("tie_vote", saved=1))
    saved = request.args.get("saved") == "1"
    return render_template("tie_vote.html", active_round=active, candidates=candidates, already=already, saved=saved, blocked=blocked)


@app.route("/admin")
@admin_required
def admin():
    teams = ordered_teams()
    judges = list_judges()
    peer_submissions = {row["voter_team_id"]: row["count"] for row in get_db().execute("SELECT voter_team_id, COUNT(*) AS count FROM peer_votes GROUP BY voter_team_id").fetchall()}
    rounds = get_db().execute("SELECT * FROM tie_break_rounds ORDER BY round_no DESC").fetchall()
    captain_query = request.args.get("captain_query", "").strip()
    captain_search_error = ""
    edit_team = get_team(request.args.get("edit_team_id")) if request.args.get("edit_team_id") else None
    edit_judge = get_judge(request.args.get("edit_judge_id")) if request.args.get("edit_judge_id") else None
    round_top_map = {round_row["id"]: tie_top_results(round_row["id"]) for round_row in rounds}
    round_top_ids_map = {round_id: [row["id"] for row in top_rows] for round_id, top_rows in round_top_map.items()}
    state = leaderboard_state()
    try:
        captain_candidates = search_feishu_users(g.user, captain_query)
    except RuntimeError as exc:
        captain_candidates = []
        captain_search_error = str(exc)
    return render_template(
        "admin.html",
        teams=teams,
        judges=judges,
        rows=state["rows"],
        peer_status=get_setting("peer_status", "not_started"),
        peer_m=get_setting("peer_m", "1"),
        peer_submissions=peer_submissions,
        peer_assignment_rows=peer_assignment_rows(),
        rounds=rounds,
        round_top_map=round_top_map,
        round_top_ids_map=round_top_ids_map,
        active_round=active_round(),
        top_tied_teams=state["top_tied_teams"],
        top_tied_ids=state["top_tied_ids"],
        needs_tie_break=state["needs_tie_break"],
        champion_id=state["champion_id"],
        champion_confirmed_at=state["champion_confirmed_at"],
        countdown_target=get_setting("event_countdown_target", "2026-07-17T09:00"),
        countdown_title=get_setting("event_countdown_title", "距离 AI 黑客松开始"),
        captain_query=captain_query,
        captain_candidates=captain_candidates,
        captain_search_error=captain_search_error,
        edit_team=edit_team,
        edit_judge=edit_judge,
        latest_closed_round=latest_closed_round(),
        tie_blacklist=tie_blacklist(),
    )


@app.route("/admin/teams", methods=["POST"])
@admin_required
def save_team():
    team_id = request.form.get("team_id")
    values = (request.form["name"].strip(), int(request.form["order_index"]), request.form["captain_open_id"].strip(), request.form["captain_name"].strip(), 1 if request.form.get("demand_vote") == "1" else 0)
    try:
        if team_id:
            get_db().execute("UPDATE teams SET name = ?, order_index = ?, captain_open_id = ?, captain_name = ?, demand_vote = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (*values, team_id))
        else:
            get_db().execute("INSERT INTO teams (name, order_index, captain_open_id, captain_name, demand_vote) VALUES (?, ?, ?, ?, ?)", values)
        get_db().commit()
        flash("队伍已保存。")
    except sqlite3.IntegrityError:
        flash("队伍保存失败：报名顺序或队长已被使用。")
    return redirect(url_for("admin"))


@app.route("/admin/teams/<int:team_id>/delete", methods=["POST"])
@admin_required
def delete_team(team_id):
    team = get_team(team_id)
    if not team:
        abort(404)
    get_db().execute("DELETE FROM teams WHERE id = ?", (team_id,))
    get_db().commit()
    if get_setting("champion_team_id", "") == str(team_id):
        set_setting("champion_team_id", "")
        set_setting("champion_confirmed_at", "")
    flash(f"已删除队伍：{team['name']}。")
    return redirect(url_for("admin"))


@app.route("/admin/peer", methods=["POST"])
@admin_required
def update_peer_settings():
    action = request.form["action"]
    try:
        if action == "set_m":
            set_setting("peer_m", validate_peer_m(request.form["peer_m"]))
        elif action in {"not_started", "open", "locked"}:
            validate_peer_m(get_setting("peer_m", "1"))
            set_setting("peer_status", action)
        else:
            abort(400)
        flash("互评设置已更新。")
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("admin"))


@app.route("/admin/countdown", methods=["POST"])
@admin_required
def update_countdown():
    title = request.form["countdown_title"].strip() or "距离 AI 黑客松开始"
    target = request.form["countdown_target"].strip()
    if not target:
        flash("请设置目标日期时间。")
        return redirect(url_for("admin"))
    set_setting("event_countdown_title", title)
    set_setting("event_countdown_target", target)
    flash("总倒计时已更新。")
    return redirect(url_for("admin"))


@app.route("/admin/judges", methods=["POST"])
@admin_required
def save_judge():
    judge_id = request.form.get("judge_id")
    values = (request.form["name"].strip(), int(request.form["order_index"]))
    try:
        if judge_id:
            get_db().execute("UPDATE judges SET name = ?, order_index = ? WHERE id = ?", (*values, judge_id))
        else:
            get_db().execute("INSERT INTO judges (name, order_index) VALUES (?, ?)", values)
        get_db().commit()
        flash("评委已保存。")
    except sqlite3.IntegrityError:
        flash("评委保存失败：序号已被使用。")
    return redirect(url_for("admin"))


@app.route("/admin/judges/<int:judge_id>/delete", methods=["POST"])
@admin_required
def delete_judge(judge_id):
    judge = get_judge(judge_id)
    if not judge:
        abort(404)
    get_db().execute("DELETE FROM judges WHERE id = ?", (judge_id,))
    get_db().commit()
    flash(f"已删除评委：{judge['name']}。")
    return redirect(url_for("admin"))


@app.route("/admin/judge-votes", methods=["POST"])
@admin_required
def save_judge_votes():
    judges = list_judges()
    teams = ordered_teams()
    for judge in judges:
        for team in teams:
            vote = 1 if request.form.get(f"judge_{judge['id']}_team_{team['id']}") == "1" else 0
            get_db().execute(
                """
                INSERT INTO judge_votes (judge_id, target_team_id, vote, entered_by_admin_open_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(judge_id, target_team_id) DO UPDATE SET vote = excluded.vote, entered_by_admin_open_id = excluded.entered_by_admin_open_id, updated_at = CURRENT_TIMESTAMP
                """,
                (judge["id"], team["id"], vote, g.user["feishu_open_id"]),
            )
    get_db().commit()
    flash("评委票已保存。")
    return redirect(url_for("admin"))


@app.route("/admin/tie/create", methods=["POST"])
@admin_required
def create_tie_round():
    if active_round():
        flash("已有进行中的加赛轮次。")
        return redirect(url_for("admin"))
    candidate_ids = request.form.getlist("candidate_id") or [str(team["id"]) for team in top_tied_teams()]
    try:
        open_tie_round(candidate_ids)
        get_db().commit()
        flash("平票加赛已开启。")
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("admin"))


@app.route("/admin/tie/<int:round_id>/close", methods=["POST"])
@admin_required
def close_tie_round(round_id):
    get_db().execute("UPDATE tie_break_rounds SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ?", (round_id,))
    get_db().commit()
    winners = tie_top_results(round_id)
    if len(winners) == 1:
        flash(f"加赛轮次已关闭，{winners[0]['name']} 当前唯一领先，可直接确认冠军。")
    elif len(winners) > 1:
        flash("加赛轮次已关闭，仍有平票，可基于本轮最高票开启下一轮。")
    else:
        flash("加赛轮次已关闭。")
    return redirect(url_for("admin"))


@app.route("/admin/tie/<int:round_id>/next", methods=["POST"])
@admin_required
def create_next_tie_round(round_id):
    if active_round():
        flash("已有进行中的加赛轮次。")
        return redirect(url_for("admin"))
    round_row = get_db().execute("SELECT * FROM tie_break_rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row:
        abort(404)
    if round_row["status"] != "closed":
        flash("请先关闭当前轮次，再开启下一轮。")
        return redirect(url_for("admin"))
    winners = tie_top_results(round_id)
    if len(winners) < 2:
        flash("该轮已产生唯一最高票，不需要开启下一轮。")
        return redirect(url_for("admin"))
    open_tie_round([row["id"] for row in winners])
    get_db().commit()
    flash("已基于上一轮最高票并列队伍开启下一轮加赛。")
    return redirect(url_for("admin"))


@app.route("/admin/champion", methods=["POST"])
@admin_required
def confirm_champion():
    team_id = request.form.get("team_id")
    if not team_id:
        abort(400)
    if not get_team(team_id):
        abort(404)
    set_setting("champion_team_id", team_id)
    set_setting("champion_confirmed_at", datetime.now().isoformat(timespec="seconds"))
    flash("冠军已确认。")
    return redirect(url_for("leaderboard"))


@app.route("/admin/blacklist", methods=["POST"])
@admin_required
def add_blacklist():
    open_id = request.form.get("open_id", "").strip()
    name = request.form.get("name", "").strip()
    if not open_id or not name:
        flash("请从飞书通讯录搜索结果中选择要加入黑名单的成员。")
        return redirect(url_for("admin"))
    try:
        get_db().execute("INSERT INTO tie_break_blacklist (open_id, name) VALUES (?, ?)", (open_id, name))
        get_db().commit()
        flash(f"已将 {name} 加入平票加赛黑名单。")
    except sqlite3.IntegrityError:
        flash(f"{name} 已在黑名单中。")
    return redirect(url_for("admin"))


@app.route("/admin/blacklist/<int:entry_id>/delete", methods=["POST"])
@admin_required
def delete_blacklist(entry_id):
    get_db().execute("DELETE FROM tie_break_blacklist WHERE id = ?", (entry_id,))
    get_db().commit()
    flash("已移出黑名单。")
    return redirect(url_for("admin"))


@app.context_processor
def inject_globals():
    judge_votes = {(row["judge_id"], row["target_team_id"]): row["vote"] for row in get_db().execute("SELECT judge_id, target_team_id, vote FROM judge_votes").fetchall()}
    return {"judge_votes": judge_votes, "tie_results": tie_results}


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True, host=APP_HOST, port=APP_PORT)
