import os
import json
import tempfile
import smtplib
import queue
import threading
import webbrowser
from datetime import datetime
from collections import deque
import numpy as np
import sounddevice as sd
import soundfile as sf
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, Response, request, jsonify, send_from_directory
from flask_cors import CORS
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
print("🎙️ Groq Whisper API klaar — klaar om te luisteren")

app = Flask(__name__)
CORS(app, resources={r"/admin/*": {"origins": "*"}})

result_queue: queue.Queue = queue.Queue()
admin_queue: queue.Queue = queue.Queue()
listen_active = threading.Event()
_listen_thread: threading.Thread | None = None
session_info: dict = {}

active_agents: dict = {}
agents_lock = threading.Lock()

ADMIN_KEY = "Voltera"

recent_sentences: deque[str] = deque(maxlen=3)

CLOSING_PHRASES = [
    "doei", "tot ziens", "fijne dag", "goedendag",
    "dag meneer", "dag mevrouw", "prettige dag",
    "tot de volgende keer", "succes verder",
    "fijn weekend", "goed weekend", "bye",
]

SYSTEM_PROMPT = """
Je bent een professionele compliance checker voor telefoongesprekken 
van het Nationaal Verduurzaming Loket (NVL) en Voltera.

CONTEXT:
Het NVL is een informatief loket dat woningeigenaren een gratis 
bespaarplan aanbiedt en doorverwijst naar Voltera.
Voltera is een installatiebedrijf, geen overheidsinstantie.
NVL en Voltera werken NIET samen met de overheid en zijn geen overheidsinstantie.
Wel is het toegestaan en waarheidsgetrouw om te zeggen dat de overheid
budget heeft vrijgesteld voor energiebesparende maatregelen (zoals via het ISDE,
Saldering of andere subsidies). Dit mag benoemd worden als feitelijke informatie,
zolang de agent zich NIET voordoet als overheidsinstantie of impliceert
dat NVL of Voltera namens de overheid opereert.
Het Warmtefonds is een onafhankelijke financieringsinstelling.
Agents mogen alleen informeren en aanraden, nooit verplichten.
Voltera heeft geen AFM of Wft vergunning.

Er zijn twee soorten agents:
- NVL agents (planners): informeren en plannen afspraak in
- Voltera agents (closers): bespreken offerte en installatie

VERBODEN HANDELINGEN (altijd Critical):

Aanvraagproces:
- Inloggen namens de klant
- Gegevens invullen voor de klant
- Documenten uploaden voor de klant
- Aanvraag samen doorlopen of meekijken op scherm

Financieel:
- Financieel advies geven over leningen of krediet
- Bemiddelen in financiering

Identiteit:
- Zich voordoen als overheidsinstantie
- Beweren samen te werken met de overheid of namens de overheid te bellen
- Impliceren dat NVL of Voltera een overheidsorganisatie is
- Zeggen dat iets wettelijk verplicht is voor de klant
- Agent vraagt DigiD gegevens op via de telefoon

UIT TE LEGGEN ALS TOEGESTAAN (geen overtreding):
- Noemen dat de overheid budget heeft vrijgesteld voor energiebesparende maatregelen
- Uitleggen dat er subsidies of regelingen bestaan (ISDE, saldering, etc.)
- Zeggen dat klanten gebruik kunnen maken van overheidsstimulering
  zolang NVL/Voltera zichzelf niet als overheidspartij neerzet

Dwang:
- Klant onder druk zetten
- Zeggen dat klant iets moet of verplicht is
- Urgentie creëren om klant te forceren

Ongepast gedrag:
- Schelden of ongepast taalgebruik

VERBODEN UITSPRAKEN (Critical):
- "Dit is de beste optie voor u"
- "Ik raad dit aan"
- "Dit past goed bij uw situatie"
- "Als u dit zo invult wordt het goedgekeurd"
- "U moet vandaag beslissen"
- "U bent verplicht"
- "Wij werken samen met de overheid"
- "Wij bellen namens de overheid"
- "Wij zijn een overheidsinstantie"
- "Dit is een overheidsprogramma"
- "Dit is verplicht vanuit de overheid"
- "Ik help u met invullen"
- "Zal ik met u meekijken"

VERBODEN UITSPRAKEN (Warning):
- "De meeste klanten kiezen"
- "Dit is slim om te doen"
- "Het is maar €X per maand"
- "Iedereen doet dit"
- "U komt hier wel voor in aanmerking"
- "Geen zorgen, dat lukt wel"
- "Dat wordt goedgekeurd"
- "U komt in aanmerking"
- "We komen sowieso installeren"
- "Dit staat vast"
- "Maandbedragen of besparingen zonder voorbehoud noemen"
- "Subsidie is bijna op" / "nog maar weinig plekken"
- "Wij regelen dat met het Warmtefonds"

TWIJFELREGEL:
Als een agent een mening geeft of richting stuurt gaat hij te ver.
De agent moet altijd neutraal blijven of doorverwijzen.
Zodra een agent een subjectief bijvoeglijk naamwoord gebruikt
zoals "goed", "slim", "perfect", "ideaal" in relatie tot 
een keuze van de klant is dit een Warning.
Normale salespraat en enthousiasme over het product is GEEN overtreding.
Alleen duidelijke en concrete overtredingen melden.
Het systeem mag niet te streng zijn — bij twijfel geen overtreding.

LOGISCH MEEDENKEN:
Beoordeel niet alleen letterlijke zinnen maar ook de intentie.
Een agent die zegt "maar dit is wel de meest gekozen optie hoor"
is ook een overtreding ook al staat de exacte zin er niet bij.
Een agent die technisch uitlegt hoe een formulier werkt 
zonder te dicteren wat de klant invult is GEEN overtreding.
Een agent die een regel uitlegt aan de klant is GEEN overtreding.
Twijfel altijd in het voordeel van de agent.

ANTWOORD ALTIJD ALS JSON:
{
  "violation": true of false,
  "severity": "critical" of "warning" of null,
  "agent_type": "nvl" of "voltera" of "onbekend",
  "rule_violated": "naam van de regel of null",
  "problematic_quote": "exacte zin van de agent of null",
  "explanation": "korte Nederlandse uitleg of null",
  "confidence": 0.0 tot 1.0
}

Confidence onder 0.80 = violation op false zetten
Critical = directe e-mail naar backoffice
Warning = alleen printen in terminal, geen e-mail
Geen overtreding = alles verwijderen
"""

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SECONDS = 8        # opnameblok duur in seconden
OVERLAP_SECONDS = 1      # overlap met vorig blok
ENERGY_THRESHOLD = 0.01  # RMS drempel; lager = stiller blok overgeslagen

_overlap_buffer: np.ndarray = np.array([], dtype=np.float32)


def record_block() -> np.ndarray:
    """Neemt een blok van BLOCK_SECONDS seconden op en geeft float32 array terug."""
    samples = int(SAMPLE_RATE * BLOCK_SECONDS)
    audio = sd.rec(samples, samplerate=SAMPLE_RATE, channels=CHANNELS,
                   dtype='float32')
    sd.wait()
    return audio.flatten()


def speech_to_text() -> str | None:
    """Neemt een 8s blok op, controleert energie en transcribeert via Groq Whisper API."""
    global _overlap_buffer

    new_block = record_block()

    # Energie check: sla stille blokken over
    rms = float(np.sqrt(np.mean(new_block ** 2)))
    if rms < ENERGY_THRESHOLD:
        _overlap_buffer = new_block[-int(SAMPLE_RATE * OVERLAP_SECONDS):]
        return None

    # Voeg overlap van vorig blok toe voor naadloze zinnen
    if _overlap_buffer.size > 0:
        audio_block = np.concatenate([_overlap_buffer, new_block])
    else:
        audio_block = new_block

    # Sla overlap op voor volgend blok
    _overlap_buffer = new_block[-int(SAMPLE_RATE * OVERLAP_SECONDS):]

    # Schrijf audio naar tijdelijk WAV-bestand voor Groq API
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        sf.write(tmp_path, audio_block, SAMPLE_RATE)
        with open(tmp_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                file=("audio.wav", audio_file, "audio/wav"),
                model="whisper-large-v3-turbo",
                language="nl",
                response_format="text",
            )
        text = response.strip() if isinstance(response, str) else str(response).strip()
        return text if text else None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def ts() -> str:
    """Geeft het huidige tijdstip terug als [HH:MM:SS]."""
    return datetime.now().strftime("[%H:%M:%S]")


def time_now() -> str:
    """Geeft het huidige tijdstip terug als HH:MM:SS (zonder haken)."""
    return datetime.now().strftime("%H:%M:%S")


def check_compliance(text: str) -> dict:
    """Stuurt tekst naar Groq met context van vorige zinnen."""
    if recent_sentences:
        context_lines = "\n".join(
            f"{i + 1}. {s}" for i, s in enumerate(recent_sentences)
        )
        user_content = (
            f"Vorige zinnen ter context:\n{context_lines}\n\n"
            f"Huidige zin om te checken:\n{text}"
        )
    else:
        user_content = text

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content.strip()

    # Verwijder eventuele markdown code fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(raw)


def _smtp_send(gmail_address: str, gmail_password: str, to: str, subject: str, body: str) -> None:
    """Hulpfunctie: verstuurt één e-mail via Gmail SMTP SSL."""
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_password)
        server.sendmail(gmail_address, to, msg.as_string())


def send_backoffice_alert(result: dict, timestamp: str) -> None:
    """Stuurt een critical-melding naar de backoffice."""
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    backoffice_email = os.getenv("BACKOFFICE_EMAIL")

    if not all([gmail_address, gmail_password, backoffice_email]):
        print(f"{ts()} ⚠️  BACKOFFICE_EMAIL niet geconfigureerd in .env")
        return

    body = (
        f"Tijdstip: {timestamp}\n"
        "Overtreding gedetecteerd in salesgesprek\n\n"
        f"Agent type: {result.get('agent_type')}\n"
        f"Regel:      {result.get('rule_violated')}\n"
        f"Ernst:      {result.get('severity')}\n"
        f"Quote:      {result.get('problematic_quote')}\n"
        f"Uitleg:     {result.get('explanation')}\n"
        f"Confidence: {result.get('confidence')}\n"
    )

    try:
        _smtp_send(gmail_address, gmail_password, backoffice_email,
                   "🚨 Compliance overtreding gedetecteerd", body)
        print(f"{ts()} 📧 Backoffice alert verstuurd naar {backoffice_email}")
    except Exception as e:
        print(f"{ts()} ❌ E-mail naar backoffice mislukt: {e}")


def print_result(result: dict, text: str) -> None:
    now = ts()
    confidence = result.get("confidence", 0.0)
    violation = result.get("violation", False)
    severity = result.get("severity")

    print(f"\n{now} 🎤 Herkende tekst: {text}")

    if violation and confidence >= 0.80:
        if severity == "critical":
            print(f"{now} 🚨 CRITICAL (confidence: {confidence:.2f})")
            print(f"   Agent  : {result.get('agent_type')}")
            print(f"   Regel  : {result.get('rule_violated')}")
            print(f"   Quote  : {result.get('problematic_quote')}")
            print(f"   Uitleg : {result.get('explanation')}")
            send_backoffice_alert(result, now)
        elif severity == "warning":
            print(f"{now} ⚠️  WARNING (confidence: {confidence:.2f})")
            print(f"   Agent  : {result.get('agent_type')}")
            print(f"   Regel  : {result.get('rule_violated')}")
            print(f"   Quote  : {result.get('problematic_quote')}")
            print(f"   Uitleg : {result.get('explanation')}")
    else:
        print(f"{now} ✅ Geen overtreding (confidence: {confidence:.2f})")


def listen_loop() -> None:
    print("🎧 Compliance BOT luistert — wacht op sessie start\n")

    while listen_active.is_set():
        try:
            print(f"{ts()} ⏳ Luisteren...")
            text = speech_to_text()

            if text is None:
                continue

            if len(text.split()) < 3:
                print(f"{ts()} ⏭️  Te kort, overgeslagen")
                continue

            text_lower = text.lower()
            closing_match = next((p for p in CLOSING_PHRASES if p in text_lower), None)
            if closing_match:
                print(f"{ts()} 👋 Gespreksafsluiting gedetecteerd: '{text}'")
                recent_sentences.clear()
                agent_name = session_info.get('agent_name', '')
                with agents_lock:
                    if agent_name in active_agents:
                        active_agents[agent_name]['status'] = 'offline'
                result_queue.put({"type": "reset", "trigger": text})
                admin_queue.put({"type": "agent_offline", "agent_name": agent_name})
                continue

            result = check_compliance(text)
            print_result(result, text)
            recent_sentences.append(text)

            agent_name = session_info.get('agent_name', '')
            now = time_now()
            with agents_lock:
                if agent_name in active_agents:
                    active_agents[agent_name]['last_active'] = now
                    active_agents[agent_name]['stats']['total'] += 1
                    violation = result.get('violation', False)
                    severity = result.get('severity')
                    if violation and result.get('confidence', 0) >= 0.80 and severity in ('warning', 'critical'):
                        active_agents[agent_name]['stats'][severity] += 1
                        v_entry = {
                            "severity": severity,
                            "rule_violated": result.get('rule_violated', ''),
                            "problematic_quote": result.get('problematic_quote', ''),
                            "explanation": result.get('explanation', ''),
                            "timestamp": now,
                        }
                        active_agents[agent_name]['violations'].append(v_entry)
                        admin_queue.put({"type": "violation", "agent_name": agent_name, "violation": v_entry,
                                         "stats": dict(active_agents[agent_name]['stats'])})
                    else:
                        active_agents[agent_name]['stats']['clean'] += 1

            result_queue.put({"spoken": text, "result": result})

        except json.JSONDecodeError as e:
            print(f"{ts()} ❌ Ongeldige JSON van Groq: {e}")
        except Exception as e:
            if listen_active.is_set():
                print(f"{ts()} ❌ Onverwachte fout: {e}")

    print(f"{ts()} 🛑 Luister loop gestopt.")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'compliance-ui.html')


@app.route('/start', methods=['POST'])
def start():
    global _listen_thread, session_info
    data = request.get_json(force=True)
    agent_name = data.get('agent_name', '')
    role = data.get('role', '')
    session_info = {'agent_name': agent_name, 'role': role}
    now = time_now()
    with agents_lock:
        active_agents[agent_name] = {
            'agent_name': agent_name,
            'role': role,
            'stats': {'total': 0, 'clean': 0, 'warning': 0, 'critical': 0},
            'violations': [],
            'connected_at': now,
            'last_active': now,
            'status': 'online',
        }
    admin_queue.put({'type': 'agent_online', 'agent_name': agent_name, 'role': role, 'connected_at': now})
    # Stop any running loop first
    listen_active.clear()
    if _listen_thread and _listen_thread.is_alive():
        _listen_thread.join(timeout=3)
    recent_sentences.clear()
    listen_active.set()
    _listen_thread = threading.Thread(target=listen_loop, daemon=True)
    _listen_thread.start()
    print(f"{ts()} ▶️  Sessie gestart — {role} · {agent_name}")
    return jsonify({'status': 'started'})


@app.route('/stop', methods=['POST'])
def stop():
    listen_active.clear()
    agent_name = session_info.get('agent_name', '')
    with agents_lock:
        if agent_name in active_agents:
            active_agents[agent_name]['status'] = 'offline'
    admin_queue.put({'type': 'agent_offline', 'agent_name': agent_name})
    print(f"{ts()} ⏹️  Sessie gestopt.")
    return jsonify({'status': 'stopped'})


@app.route('/events')
def events():
    def stream():
        while True:
            try:
                data = result_queue.get(timeout=25)
                if data.get('type') == 'reset':
                    yield f"event: reset\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(
        stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Admin helper ──────────────────────────────────────────────────────────────

def _require_admin_key():
    """Controleert X-Admin-Key header. Geeft None terug bij succes, Response bij fout."""
    key = request.headers.get('X-Admin-Key', '')
    if key != ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


# ── Admin API endpoints ───────────────────────────────────────────────────────

@app.route('/admin/agents')
def admin_agents():
    err = _require_admin_key()
    if err:
        return err
    with agents_lock:
        data = {name: dict(agent) for name, agent in active_agents.items()}
    return jsonify(data)


@app.route('/admin/agents/<agent_name>')
def admin_agent_detail(agent_name):
    err = _require_admin_key()
    if err:
        return err
    with agents_lock:
        agent = active_agents.get(agent_name)
        if agent is None:
            return jsonify({'error': 'Agent niet gevonden'}), 404
        data = dict(agent)
    return jsonify(data)


@app.route('/admin/stream')
def admin_stream():
    key = request.args.get('key', '')
    if key != ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401

    # Geef een per-subscriber queue om fan-out te ondersteunen
    sub_queue: queue.Queue = queue.Queue()

    def producer():
        while True:
            try:
                item = admin_queue.get(timeout=25)
                sub_queue.put(item)
                # Fan-out: zet het item terug zodat andere subscribers het ook zien
                # (simpele single-subscriber aanpak; voor multi-admin uitbreiden met pub/sub)
            except queue.Empty:
                sub_queue.put(None)  # keepalive trigger

    prod_thread = threading.Thread(target=producer, daemon=True)
    prod_thread.start()

    def stream():
        while True:
            try:
                item = sub_queue.get(timeout=30)
                if item is None:
                    yield ": keepalive\n\n"
                else:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(
        stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    threading.Timer(1.0, lambda: webbrowser.open('http://localhost:5000')).start()
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
