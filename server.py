import os
import json
import sqlite3
import queue
import threading
import time
from datetime import datetime, date
from flask import Flask, Response, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

ADMIN_KEY = "Voltera"
AGENT_KEY = os.getenv("AGENT_KEY", "NVL2026")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance.db")

app = Flask(__name__)
CORS(app)

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


# ── Database ──────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
        """)
        # Bij elke serverstart: alle agents op offline zetten zodat de UI leeg begint
        conn.execute("UPDATE agents SET status = 'offline'")
        conn.commit()
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
    agent_name = data.get("agent_name", "").strip()
    role = data.get("role", "").strip()
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
    finally:
        conn.close()
    broadcast({"type": "agent_online", "agent_name": agent_name,
               "role": role, "connected_at": now})
    print(f"{ts()} 🟢 Agent online: {agent_name} ({role})")
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
    print(f"{ts()} 🔴 Agent offline: {agent_name}")
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
    print(f"{ts()} 🔄 Gesprek reset: {agent_name}")
    return jsonify({"status": "ok"})


@app.route("/api/violation", methods=["POST"])
def add_violation():
    err = require_agent_key()
    if err:
        return err
    data = request.get_json(force=True)
    agent_name = data.get("agent_name", "").strip()
    role = data.get("role", "").strip()
    severity = data.get("severity", "").strip()
    rule_violated = data.get("rule_violated", "")
    problematic_quote = data.get("problematic_quote", "")
    explanation = data.get("explanation", "")
    timestamp = data.get("timestamp", time_now())
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
            print(f"{ts()} ✅ Clean check van {agent_name}")
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
    icon = "🚨" if severity == "critical" else "⚠️"
    print(f"{ts()} {icon} Violation van {agent_name}: {rule_violated}")
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
                            print(f"{ts()} ⏰ Agent automatisch offline: {name} (geen heartbeat)")
                    except (ValueError, TypeError):
                        pass
            finally:
                conn.close()
        except Exception as e:
            print(f"{ts()} ❌ Offline check fout: {e}")


if __name__ == "__main__":
    init_db()
    print("✅ Centrale compliance server gestart op poort 8000")
    print(f"📊 Database: {DB_PATH}")
    print("🔑 Admin toegang via: http://localhost:8000")
    offline_thread = threading.Thread(target=_offline_check_loop, daemon=True)
    offline_thread.start()
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
