import os
import sys
import json
import csv
import io
import sqlite3
import queue
import threading
import time
import logging
import logging.handlers
from datetime import datetime, date, timedelta
from collections import defaultdict
from functools import wraps
from flask import Flask, Response, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

ADMIN_KEY = "Voltera"
AGENT_KEY = os.getenv("AGENT_KEY", "NVL2026")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance.db")
VERSION = "1.0.0"

# ── Logging setup ─────────────────────────────────────────────────────────────
_LOG_DIR = os.path.dirname(os.path.abspath(__file__))

_server_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, "server.log"),
    maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_server_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

_error_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, "errors.log"),
    maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_error_handler.setLevel(logging.ERROR)
_error_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

log = logging.getLogger("server")
log.setLevel(logging.INFO)
log.addHandler(_server_handler)
log.addHandler(_error_handler)
log.addHandler(_console_handler)

log.info("=== NVL Compliance Server start (v%s) ===", VERSION)

app = Flask(__name__)
CORS(app)


# ── Rate limiting (60 req/min/IP) ─────────────────────────────────────────────
_rate_buckets: dict[str, list] = defaultdict(list)
_rate_lock = threading.Lock()
RATE_LIMIT = 60
RATE_WINDOW = 60  # seconds


def _check_rate_limit() -> bool:
    """Returns True if request is within rate limit, False if exceeded."""
    ip = request.remote_addr or "unknown"
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        # Remove old entries
        _rate_buckets[ip] = [t for t in bucket if now - t < RATE_WINDOW]
        if len(_rate_buckets[ip]) >= RATE_LIMIT:
            return False
        _rate_buckets[ip].append(now)
    return True


@app.before_request
def rate_limit_check():
    if not _check_rate_limit():
        log.warning("Rate limit overschreden door %s", request.remote_addr)
        return jsonify({"error": "Te veel verzoeken. Probeer later opnieuw."}), 429


# ── Input validation / sanitization ───────────────────────────────────────────
def sanitize_text(value: str, max_len: int = 500) -> str:
    """Strip HTML tags and limit length."""
    if not isinstance(value, str):
        return ""
    import re
    clean = re.sub(r"<[^>]+>", "", value)
    return clean[:max_len].strip()


def validate_agent_name(name: str) -> str | None:
    """Returns sanitized name or None if invalid."""
    name = sanitize_text(name, 100)
    if not name or len(name) < 1:
        return None
    return name


# ── Admin action logging ──────────────────────────────────────────────────────
def log_admin_action(action: str, detail: str = "") -> None:
    ip = request.remote_addr or "unknown"
    log.info("ADMIN ACTION [%s] %s %s", ip, action, detail)

# ── SSE fan-out ───────────────────────────────────────────────────────────────
_subscribers: list = []
_subscribers_lock = threading.Lock()


def broadcast(data: dict) -> None:
    msg = json.dumps(data, ensure_ascii=False)
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


# ── SSE rate limiting (max 50 events/sec to any single subscriber) ────────────
_sse_event_count = 0
_sse_event_lock = threading.Lock()


# ── Database ──────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                agent_name   TEXT PRIMARY KEY,
                role         TEXT,
                status       TEXT DEFAULT 'offline',
                connected_at TEXT,
                last_active  TEXT,
                total_checks   INTEGER DEFAULT 0,
                total_clean    INTEGER DEFAULT 0,
                total_warning  INTEGER DEFAULT 0,
                total_critical INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS violations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name       TEXT,
                role             TEXT,
                severity         TEXT,
                rule_violated    TEXT,
                problematic_quote TEXT,
                explanation      TEXT,
                timestamp        TEXT,
                session_date     TEXT
            );
            CREATE TABLE IF NOT EXISTS admin_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ip        TEXT,
                action    TEXT,
                detail    TEXT
            );
        """)
        # Bij elke serverstart: alle agents op offline zetten zodat de UI leeg begint
        conn.execute("UPDATE agents SET status = 'offline'")
        conn.commit()
        log.info("Database geinitialiseerd: %s", DB_PATH)
    except Exception as e:
        log.error("Database init fout: %s", e)
        raise
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────
def ts() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def time_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def require_agent_key():
    if request.headers.get("X-Agent-Key", "") != AGENT_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def require_admin_key():
    if request.headers.get("X-Admin-Key", "") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


# ── Agent endpoints (called by agent PCs) ─────────────────────────────────────

@app.route("/api/agent/online", methods=["POST"])
def agent_online():
    err = require_agent_key()
    if err:
        return err
    data = request.get_json(force=True)
    agent_name = validate_agent_name(data.get("agent_name", ""))
    role = sanitize_text(data.get("role", ""), 50)
    if not agent_name:
        return jsonify({"error": "agent_name vereist"}), 400
    now = time_now()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO agents
                (agent_name, role, status, connected_at, last_active,
                 total_checks, total_clean, total_warning, total_critical)
            VALUES (?, ?, 'online', ?, ?, 0, 0, 0, 0)
            ON CONFLICT(agent_name) DO UPDATE SET
                role=excluded.role,
                status='online',
                connected_at=excluded.connected_at,
                last_active=excluded.last_active,
                total_checks=0,
                total_clean=0,
                total_warning=0,
                total_critical=0
        """, (agent_name, role, now, now))
        conn.commit()
    except Exception as e:
        log.error("DB fout bij agent_online: %s", e)
        return jsonify({"error": "Database fout"}), 500
    finally:
        conn.close()
    broadcast({"type": "agent_online", "agent_name": agent_name,
               "role": role, "connected_at": now})
    log.info("Agent online: %s (%s)", agent_name, role)
    return jsonify({"status": "ok"})


@app.route("/api/agent/offline", methods=["POST"])
def agent_offline():
    err = require_agent_key()
    if err:
        return err
    data = request.get_json(force=True)
    agent_name = data.get("agent_name", "").strip()
    conn = get_db()
    try:
        conn.execute("UPDATE agents SET status='offline' WHERE agent_name=?",
                     (agent_name,))
        conn.commit()
    finally:
        conn.close()
    broadcast({"type": "agent_offline", "agent_name": agent_name})
    log.info("Agent offline: %s", agent_name)
    return jsonify({"status": "ok"})


@app.route("/api/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    err = require_agent_key()
    if err:
        return err
    data = request.get_json(force=True)
    agent_name = data.get("agent_name", "").strip()
    role = data.get("role", "").strip()
    now = time_now()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO agents (agent_name, role, status, connected_at, last_active)
            VALUES (?, ?, 'online', ?, ?)
            ON CONFLICT(agent_name) DO UPDATE SET
                last_active=excluded.last_active,
                status='online'
        """, (agent_name, role, now, now))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/agent/reset", methods=["POST"])
def agent_reset():
    err = require_agent_key()
    if err:
        return err
    data = request.get_json(force=True)
    agent_name = data.get("agent_name", "").strip()
    now = time_now()
    conn = get_db()
    try:
        conn.execute("""
            UPDATE agents
            SET total_checks=0, total_clean=0,
                total_warning=0, total_critical=0, last_active=?
            WHERE agent_name=?
        """, (now, agent_name))
        conn.commit()
    finally:
        conn.close()
    broadcast({"type": "agent_reset", "agent_name": agent_name})
    log.info("Gesprek reset: %s", agent_name)
    return jsonify({"status": "ok"})


@app.route("/api/violation", methods=["POST"])
def add_violation():
    err = require_agent_key()
    if err:
        return err
    data = request.get_json(force=True)
    agent_name = sanitize_text(data.get("agent_name", ""), 100)
    role = sanitize_text(data.get("role", ""), 50)
    severity = sanitize_text(data.get("severity", ""), 20)
    rule_violated = sanitize_text(data.get("rule_violated", ""), 500)
    problematic_quote = sanitize_text(data.get("problematic_quote", ""), 1000)
    explanation = sanitize_text(data.get("explanation", ""), 1000)
    timestamp = sanitize_text(data.get("timestamp", time_now()), 20)
    session_date = date.today().isoformat()

    conn = get_db()
    try:
        if severity == "clean":
            # Clean check — alleen counter ophogen, geen violation opslaan
            conn.execute("""
                UPDATE agents
                SET total_checks = total_checks + 1,
                    total_clean = total_clean + 1,
                    last_active = ?
                WHERE agent_name = ?
            """, (timestamp, agent_name))
            conn.commit()
            row = conn.execute(
                "SELECT total_checks, total_clean, total_warning, total_critical "
                "FROM agents WHERE agent_name=?",
                (agent_name,)
            ).fetchone()
            stats = {
                "total": row["total_checks"],
                "clean": row["total_clean"],
                "warning": row["total_warning"],
                "critical": row["total_critical"],
            } if row else {"total": 0, "clean": 0, "warning": 0, "critical": 0}
            conn.close()
            broadcast({"type": "agent_clean", "agent_name": agent_name, "stats": stats})
            log.info("Clean check van %s", agent_name)
            return jsonify({"status": "ok"})

        conn.execute("""
            INSERT INTO violations
                (agent_name, role, severity, rule_violated, problematic_quote,
                 explanation, timestamp, session_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (agent_name, role, severity, rule_violated, problematic_quote,
              explanation, timestamp, session_date))
        # Bewaar max 10 overtredingen per agent — verwijder oudste
        conn.execute("""
            DELETE FROM violations WHERE agent_name = ? AND id NOT IN (
                SELECT id FROM violations WHERE agent_name = ?
                ORDER BY id DESC LIMIT 10
            )
        """, (agent_name, agent_name))
        if severity == "warning":
            conn.execute("""
                UPDATE agents
                SET total_checks = total_checks + 1,
                    total_warning = total_warning + 1,
                    last_active = ?
                WHERE agent_name = ?
            """, (timestamp, agent_name))
        elif severity == "critical":
            conn.execute("""
                UPDATE agents
                SET total_checks = total_checks + 1,
                    total_critical = total_critical + 1,
                    last_active = ?
                WHERE agent_name = ?
            """, (timestamp, agent_name))
        conn.commit()
        row = conn.execute(
            "SELECT total_checks, total_clean, total_warning, total_critical "
            "FROM agents WHERE agent_name=?",
            (agent_name,)
        ).fetchone()
        stats = {
            "total": row["total_checks"],
            "clean": row["total_clean"],
            "warning": row["total_warning"],
            "critical": row["total_critical"],
        } if row else {"total": 0, "clean": 0, "warning": 0, "critical": 0}
    finally:
        conn.close()

    v_entry = {
        "severity": severity,
        "rule_violated": rule_violated,
        "problematic_quote": problematic_quote,
        "explanation": explanation,
        "timestamp": timestamp,
    }
    broadcast({
        "type": "violation",
        "agent_name": agent_name,
        "violation": v_entry,
        "stats": stats,
    })
    icon = "CRITICAL" if severity == "critical" else "WARNING"
    log.info("Violation van %s: [%s] %s", agent_name, icon, rule_violated)
    return jsonify({"status": "ok"})


@app.route("/api/agents/<agent_name>", methods=["DELETE"])
def delete_agent(agent_name):
    err = require_admin_key()
    if err:
        return err
    conn = get_db()
    try:
        conn.execute("DELETE FROM violations WHERE agent_name = ?", (agent_name,))
        conn.execute("DELETE FROM agents WHERE agent_name = ?", (agent_name,))
        conn.commit()
    finally:
        conn.close()
    broadcast({"type": "agent_deleted", "agent_name": agent_name})
    log_admin_action("DELETE_AGENT", agent_name)
    return jsonify({"status": "deleted"})


@app.route("/api/agents")
def get_agents():
    err = require_admin_key()
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY status DESC, agent_name"
        ).fetchall()
        agents = {}
        for row in rows:
            a = dict(row)
            a["stats"] = {
                "total":    a.pop("total_checks"),
                "clean":    a.pop("total_clean"),
                "warning":  a.pop("total_warning"),
                "critical": a.pop("total_critical"),
            }
            agents[a["agent_name"]] = a
    finally:
        conn.close()
    return jsonify(agents)


@app.route("/api/agents/<agent_name>/violations")
def get_agent_violations(agent_name):
    err = require_admin_key()
    if err:
        return err
    date_filter = request.args.get("date")
    conn = get_db()
    try:
        if date_filter:
            rows = conn.execute(
                "SELECT * FROM violations WHERE agent_name=? AND session_date=? "
                "ORDER BY id DESC",
                (agent_name, date_filter)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM violations WHERE agent_name=? ORDER BY id DESC",
                (agent_name,)
            ).fetchall()
        violations = [dict(row) for row in rows]
    finally:
        conn.close()
    return jsonify(violations)


@app.route("/api/stream")
def admin_stream():
    if request.args.get("key", "") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    sub_q: queue.Queue = queue.Queue(maxsize=200)
    with _subscribers_lock:
        _subscribers.append(sub_q)

    def stream():
        try:
            while True:
                try:
                    msg = sub_q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _subscribers_lock:
                if sub_q in _subscribers:
                    _subscribers.remove(sub_q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── History / export / status endpoints ───────────────────────────────────────

@app.route("/api/violations/history")
def violations_history():
    """Violations met filters: ?date=YYYY-MM-DD&agent=naam&severity=critical|warning"""
    err = require_admin_key()
    if err:
        return err
    date_filter = request.args.get("date")
    agent_filter = request.args.get("agent")
    severity_filter = request.args.get("severity")

    query = "SELECT * FROM violations WHERE 1=1"
    params = []
    if date_filter:
        query += " AND session_date = ?"
        params.append(sanitize_text(date_filter, 10))
    if agent_filter:
        query += " AND agent_name = ?"
        params.append(sanitize_text(agent_filter, 100))
    if severity_filter and severity_filter in ("critical", "warning"):
        query += " AND severity = ?"
        params.append(severity_filter)
    query += " ORDER BY id DESC LIMIT 500"

    conn = get_db()
    try:
        rows = conn.execute(query, params).fetchall()
        violations = [dict(row) for row in rows]
    except Exception as e:
        log.error("History query fout: %s", e)
        return jsonify({"error": "Database fout"}), 500
    finally:
        conn.close()
    log_admin_action("VIEW_HISTORY", f"filters: date={date_filter}, agent={agent_filter}, severity={severity_filter}")
    return jsonify(violations)


@app.route("/api/violations/export")
def violations_export():
    """CSV export van violations met dezelfde filters als history."""
    err = require_admin_key()
    if err:
        return err
    date_filter = request.args.get("date")
    agent_filter = request.args.get("agent")
    severity_filter = request.args.get("severity")

    query = "SELECT * FROM violations WHERE 1=1"
    params = []
    if date_filter:
        query += " AND session_date = ?"
        params.append(sanitize_text(date_filter, 10))
    if agent_filter:
        query += " AND agent_name = ?"
        params.append(sanitize_text(agent_filter, 100))
    if severity_filter and severity_filter in ("critical", "warning"):
        query += " AND severity = ?"
        params.append(severity_filter)
    query += " ORDER BY id DESC"

    conn = get_db()
    try:
        rows = conn.execute(query, params).fetchall()
    except Exception as e:
        log.error("Export query fout: %s", e)
        return jsonify({"error": "Database fout"}), 500
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["id", "agent_name", "role", "severity", "rule_violated",
                     "problematic_quote", "explanation", "timestamp", "session_date"])
    for row in rows:
        r = dict(row)
        writer.writerow([r.get("id"), r.get("agent_name"), r.get("role"),
                         r.get("severity"), r.get("rule_violated"),
                         r.get("problematic_quote"), r.get("explanation"),
                         r.get("timestamp"), r.get("session_date")])

    log_admin_action("EXPORT_CSV", f"{len(rows)} rijen")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=violations_{date.today().isoformat()}.csv"},
    )


@app.route("/api/status")
def system_status():
    """Systeem status: agents online/offline, totaal violations, server uptime."""
    err = require_admin_key()
    if err:
        return err
    conn = get_db()
    try:
        agents_online = conn.execute("SELECT COUNT(*) FROM agents WHERE status='online'").fetchone()[0]
        agents_total = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        violations_today = conn.execute(
            "SELECT COUNT(*) FROM violations WHERE session_date=?",
            (date.today().isoformat(),)
        ).fetchone()[0]
        violations_total = conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
        critical_today = conn.execute(
            "SELECT COUNT(*) FROM violations WHERE session_date=? AND severity='critical'",
            (date.today().isoformat(),)
        ).fetchone()[0]
    except Exception as e:
        log.error("Status query fout: %s", e)
        return jsonify({"error": "Database fout"}), 500
    finally:
        conn.close()

    uptime_s = int(time.time() - _server_start_time)
    return jsonify({
        "version": VERSION,
        "uptime_seconds": uptime_s,
        "agents_online": agents_online,
        "agents_total": agents_total,
        "violations_today": violations_today,
        "violations_total": violations_total,
        "critical_today": critical_today,
        "sse_subscribers": len(_subscribers),
    })


@app.route("/api/violations/dates")
def violation_dates():
    """Geeft lijst van unieke session_dates terug voor de date picker."""
    err = require_admin_key()
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT session_date FROM violations ORDER BY session_date DESC LIMIT 90"
        ).fetchall()
        dates = [row["session_date"] for row in rows]
    finally:
        conn.close()
    return jsonify(dates)


@app.route("/")
def serve_admin():
    return send_from_directory(".", "admin.html")


# ── Offline detectie ──────────────────────────────────────────────────────────

def _offline_check_loop() -> None:
    """Markeert agents als offline als ze meer dan 90 seconden geen heartbeat sturen."""
    while True:
        time.sleep(60)
        try:
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT agent_name, last_active FROM agents WHERE status='online'"
                ).fetchall()
                for row in rows:
                    name, last_active = row["agent_name"], row["last_active"]
                    if not last_active:
                        continue
                    try:
                        today = datetime.now()
                        last_dt = datetime.strptime(last_active, "%H:%M:%S").replace(
                            year=today.year, month=today.month, day=today.day
                        )
                        if (today - last_dt).total_seconds() > 90:
                            conn.execute(
                                "UPDATE agents SET status='offline' WHERE agent_name=?",
                                (name,)
                            )
                            conn.commit()
                            broadcast({"type": "agent_offline", "agent_name": name})
                            log.info("Agent automatisch offline: %s (geen heartbeat)", name)
                    except (ValueError, TypeError):
                        pass
            finally:
                conn.close()
        except Exception as e:
            log.error("Offline check fout: %s", e)


def _retention_cleanup() -> None:
    """Verwijdert violations ouder dan 30 dagen. Draait eens per uur."""
    while True:
        time.sleep(3600)
        try:
            cutoff = (date.today() - timedelta(days=30)).isoformat()
            conn = get_db()
            try:
                result = conn.execute(
                    "DELETE FROM violations WHERE session_date < ?", (cutoff,)
                )
                deleted = result.rowcount
                conn.commit()
                if deleted > 0:
                    log.info("Retention cleanup: %d oude violations verwijderd (voor %s)", deleted, cutoff)
            finally:
                conn.close()
        except Exception as e:
            log.error("Retention cleanup fout: %s", e)


_server_start_time = time.time()


if __name__ == "__main__":
    init_db()
    log.info("Centrale compliance server gestart op poort 8000")
    log.info("Database: %s", DB_PATH)
    log.info("Admin toegang via: http://localhost:8000")
    offline_thread = threading.Thread(target=_offline_check_loop, daemon=True)
    offline_thread.start()
    retention_thread = threading.Thread(target=_retention_cleanup, daemon=True)
    retention_thread.start()
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
