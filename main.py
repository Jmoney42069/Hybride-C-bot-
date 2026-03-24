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
import requests as http_requests
from flask import Flask, Response, request, jsonify, send_from_directory
from groq import Groq
from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
cerebras_client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
print("🎙️ Groq Whisper + Cerebras LLM klaar — klaar om te luisteren")

CENTRAL_SERVER = os.getenv("CENTRAL_SERVER", "http://localhost:8000")
AGENT_KEY = os.getenv("AGENT_KEY", "NVL2026")

app = Flask(__name__)

result_queue: queue.Queue = queue.Queue()
listen_active = threading.Event()
_listen_thread: threading.Thread | None = None
session_info: dict = {}


def report_to_server(endpoint: str, data: dict) -> None:
    """Stuurt data naar de centrale server. Bij fout: alleen waarschuwing, geen crash."""
    try:
        http_requests.post(
            f"{CENTRAL_SERVER}{endpoint}",
            json=data,
            headers={"X-Agent-Key": AGENT_KEY},
            timeout=3,
        )
    except Exception as e:
        print(f"[!] Centrale server niet bereikbaar ({endpoint}): {e}")


def _heartbeat_loop() -> None:
    """Stuurt elke 30 seconden een heartbeat naar de centrale server."""
    import time
    while True:
        time.sleep(30)
        if listen_active.is_set() and session_info.get('agent_name'):
            report_to_server('/api/agent/heartbeat', {
                'agent_name': session_info['agent_name'],
                'role': session_info.get('role', ''),
            })

recent_sentences: deque[str] = deque(maxlen=2)
_call_transcript: list[str] = []  # verzamelt alle zinnen van het huidige gesprek
_call_active: bool = False  # wordt True bij begroeting, False na afsluiting

GREETING_PHRASES = [
    "hallo", "goeiedag", "goedendag", "goedemiddag", "goedemorgen",
    "goedeavond", "goeiemorgen", "goeiemiddag", "goeieavond",
    "hey ", "hoi ", "welkom", "goed dat u belt",
    "waarmee kan ik u helpen", "waar kan ik u mee helpen",
    "fijn dat u belt", "u spreekt met",
]

CLOSING_PHRASES = [
    "doei", "tot ziens", "fijne dag", "goedendag",
    "dag meneer", "dag mevrouw", "prettige dag",
    "tot de volgende keer", "succes verder",
    "fijn weekend", "goed weekend", "bye",
]

# Whisper hallucinaties — spooktekst die het model genereert bij stilte/ruis
WHISPER_GHOSTS = [
    "ondertitels", "ondertiteling", "bedankt voor het kijken",
    "thanks for watching", "subscribe", "like and subscribe",
    "music", "muziek", "applaus", "gelach",
]

# Extreme keywords — alleen deze triggeren een INSTANT alert (geen LLM nodig)
INSTANT_CRITICAL_KEYWORDS = [
    # scheldwoorden
    "lul", "klootzak", "idioot", "achterlijk",
    "kut", "godver", "kanker", "hoer", "debiel",
    # DigiD fraude
    "digid", "ik log even in", "ik log in",
    # Direct invullen voor klant
    "ik vul het voor u in", "ik vul het in", "zal ik invullen",
    "ik upload dat", "ik kijk even mee",
]

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SECONDS = 4        # opnameblok duur in seconden
OVERLAP_SECONDS = 1      # overlap met vorig blok
ENERGY_THRESHOLD = 0.01  # RMS drempel; lager = stiller blok overgeslagen

_overlap_buffer: np.ndarray = np.array([], dtype=np.float32)
_audio_queue: queue.Queue = queue.Queue(maxsize=4)


def record_block() -> np.ndarray:
    """Neemt een blok van BLOCK_SECONDS seconden op en geeft float32 array terug."""
    samples = int(SAMPLE_RATE * BLOCK_SECONDS)
    audio = sd.rec(samples, samplerate=SAMPLE_RATE, channels=CHANNELS,
                   dtype='float32')
    sd.wait()
    return audio.flatten()


def _recorder_loop() -> None:
    """Recorder thread: neemt continu blokken op en plaatst ze in de audio queue."""
    global _overlap_buffer
    while listen_active.is_set():
        new_block = record_block()
        rms = float(np.sqrt(np.mean(new_block ** 2)))
        if rms < ENERGY_THRESHOLD:
            _overlap_buffer = new_block[-int(SAMPLE_RATE * OVERLAP_SECONDS):]
            continue
        if _overlap_buffer.size > 0:
            audio_block = np.concatenate([_overlap_buffer, new_block])
        else:
            audio_block = new_block
        _overlap_buffer = new_block[-int(SAMPLE_RATE * OVERLAP_SECONDS):]
        try:
            _audio_queue.put_nowait(audio_block)
        except queue.Full:
            pass  # verwerker loopt achter, drop blok


def transcribe_block(audio_block: np.ndarray) -> str | None:
    """Transcribeert één audio blok via Groq Whisper."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        sf.write(tmp_path, audio_block, SAMPLE_RATE)
        with open(tmp_path, "rb") as audio_file:
            response = groq_client.audio.transcriptions.create(
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


POST_CALL_PROMPT = """Je bent een compliance checker voor NVL/Voltera salesgesprekken.
Je krijgt het VOLLEDIGE transcript van één telefoongesprek.
Analyseer uitsluitend wat de AGENT zegt. Beoordeel intentie en context.

Doceren (uitleggen, informeren) = toegestaan
Sturen/Handelen (adviseren, garanderen, invullen, druk) = overtreding

CRITICAL: aanvraag overnemen, financieel advies/sturing, garanties over goedkeuring, druk uitoefenen, overheid/partnerschap claimen, ongepast taalgebruik, klant verplichten.
WARNING: kosten bagatelliseren zonder voorbehoud, groepsdruk, valse urgentie, spreken namens Warmtefonds.

TOEGESTAAN: uitleggen hoe formulier werkt, subsidies feitelijk noemen, voorrekenen met voorbehoud, AFM disclaimer, stappen Warmtefonds uitleggen, eindcontrole vraag.

Belangrijk: "het is €X per maand mits goedgekeurd" is GEEN overtreding (voorbehoud aanwezig).

Antwoord als JSON array van overtredingen. Lege array als geen overtredingen:
[{"severity":"critical"|"warning","rule_violated":"str","problematic_quote":"exacte zin","explanation":"korte NL uitleg","confidence":float}]

Confidence <0.80 = niet opnemen. Twijfel = in voordeel van de agent."""


def analyse_full_transcript(transcript: list[str]) -> list[dict]:
    """Post-call analyse: stuurt het hele transcript naar qwen-235B."""
    if not transcript:
        return []

    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(transcript))

    try:
        response = cerebras_client.chat.completions.create(
            model="qwen-3-235b-a22b-instruct-2507",
            messages=[
                {"role": "system", "content": POST_CALL_PROMPT},
                {"role": "user", "content": numbered},
            ],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1])
        parsed = json.loads(raw)
        # Kan een dict zijn met key 'violations' of direct een list
        if isinstance(parsed, dict):
            parsed = parsed.get("violations", parsed.get("results", []))
        if not isinstance(parsed, list):
            parsed = [parsed] if parsed else []
        return [v for v in parsed if v.get("confidence", 0) >= 0.80]
    except Exception as e:
        print(f"{ts()} ❌ Post-call analyse fout: {e}")
        return []


def _process_post_call(transcript: list[str]) -> None:
    """Draait post-call analyse in een aparte thread zodat de main loop niet blokkeert."""
    print(f"\n{ts()} 📊 Post-call analyse gestart ({len(transcript)} zinnen)...")
    violations = analyse_full_transcript(transcript)

    if not violations:
        print(f"{ts()} ✅ Geen overtredingen in dit gesprek")
        # Rapporteer schone zinnen naar server voor total_clean counter
        report_to_server('/api/violation', {
            'agent_name': session_info.get('agent_name', ''),
            'role': session_info.get('role', ''),
            'severity': 'clean',
            'rule_violated': '',
            'problematic_quote': '',
            'explanation': f'{len(transcript)} zinnen geanalyseerd — geen overtredingen',
            'timestamp': time_now(),
        })
        result_queue.put({"type": "post_call", "violations": [], "total_sentences": len(transcript)})
        return

    print(f"{ts()} 🚨 {len(violations)} overtreding(en) gevonden:")
    for v in violations:
        sev = v.get("severity", "?")
        icon = "🚨" if sev == "critical" else "⚠️"
        print(f"   {icon} [{sev.upper()}] {v.get('rule_violated', '?')}")
        print(f"      Quote: {v.get('problematic_quote', '')}")
        print(f"      Uitleg: {v.get('explanation', '')}")

        # Rapporteer elke overtreding naar de server
        report_to_server('/api/violation', {
            'agent_name': session_info.get('agent_name', ''),
            'role': session_info.get('role', ''),
            'severity': sev,
            'rule_violated': v.get('rule_violated', ''),
            'problematic_quote': v.get('problematic_quote', ''),
            'explanation': v.get('explanation', ''),
            'timestamp': time_now(),
        })

        # Critical → backoffice email
        if sev == "critical":
            send_backoffice_alert(v, ts())

    result_queue.put({"type": "post_call", "violations": violations, "total_sentences": len(transcript)})
    print(f"{ts()} ✅ Post-call analyse compleet")


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
        f"Agent type: {session_info.get('role', '?')}\n"
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


def listen_loop() -> None:
    print("🎧 Compliance BOT luistert — hybride modus\n")

    # Leeg de queue van eventuele oude blokken
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break

    # Reset transcript voor nieuw gesprek
    global _call_active
    _call_transcript.clear()
    _call_active = False

    # Start recorder thread — neemt continu op terwijl wij verwerken
    rec_thread = threading.Thread(target=_recorder_loop, daemon=True)
    rec_thread.start()

    print(f"{ts()} ⏳ Wacht op begroeting om gesprek te starten...")

    while listen_active.is_set():
        try:
            try:
                audio_block = _audio_queue.get(timeout=1)
            except queue.Empty:
                continue

            text = transcribe_block(audio_block)

            if text is None:
                continue

            if len(text.split()) < 3:
                continue

            # Whisper hallucinatie check
            text_lower = text.lower()
            if any(ghost in text_lower for ghost in WHISPER_GHOSTS):
                print(f"{ts()} 👻 Whisper hallucinatie gefilterd")
                continue

            # ── INSTANT ALERT: altijd actief, ook buiten gesprek ──
            instant_match = next((kw for kw in INSTANT_CRITICAL_KEYWORDS if kw in text_lower), None)
            if instant_match:
                print(f"{ts()} 🚨 INSTANT CRITICAL: '{instant_match}' in: {text}")
                instant_result = {
                    "severity": "critical",
                    "rule_violated": "Instant detectie: " + instant_match,
                    "problematic_quote": text,
                    "explanation": f"Directe overtreding gedetecteerd: '{instant_match}'",
                    "confidence": 0.99,
                }
                report_to_server('/api/violation', {
                    'agent_name': session_info.get('agent_name', ''),
                    'role': session_info.get('role', ''),
                    'severity': 'critical',
                    'rule_violated': instant_result['rule_violated'],
                    'problematic_quote': text,
                    'explanation': instant_result['explanation'],
                    'timestamp': time_now(),
                })
                send_backoffice_alert(instant_result, ts())
                result_queue.put({"spoken": text, "result": {"violation": True, **instant_result}})

            # ── GREETING: activeer gesprek als nog niet actief ──
            if not _call_active:
                greeting_match = next((g for g in GREETING_PHRASES if g in text_lower), None)
                if greeting_match:
                    _call_active = True
                    _call_transcript.clear()
                    _call_transcript.append(text)
                    print(f"\n{ts()} 📞 Nieuw gesprek gedetecteerd: '{text}'")
                    result_queue.put({"type": "call_start", "trigger": text})
                    result_queue.put({"spoken": text, "result": {
                        "violation": False, "severity": None, "confidence": 1.0,
                        "rule_violated": None, "problematic_quote": None, "explanation": None,
                    }})
                else:
                    # Geen actief gesprek en geen begroeting → skip
                    print(f"{ts()} 💤 Buiten gesprek: {text[:50]}...")
                continue

            # ── ACTIEF GESPREK: verzamel transcript ──
            _call_transcript.append(text)
            print(f"{ts()} 🎤 [{len(_call_transcript)}] {text}")
            result_queue.put({"spoken": text, "result": {
                "violation": False, "severity": None, "confidence": 1.0,
                "rule_violated": None, "problematic_quote": None, "explanation": None,
            }})

            # ── CLOSING PHRASE → trigger post-call analyse ──
            closing_match = next((p for p in CLOSING_PHRASES if p in text_lower), None)
            if closing_match:
                print(f"{ts()} 👋 Gespreksafsluiting gedetecteerd: '{text}'")
                print(f"{ts()} 📄 Transcript bevat {len(_call_transcript)} zinnen")

                # Kopieer transcript en start post-call analyse in aparte thread
                transcript_copy = list(_call_transcript)
                _call_transcript.clear()
                _call_active = False
                recent_sentences.clear()

                threading.Thread(
                    target=_process_post_call,
                    args=(transcript_copy,),
                    daemon=True
                ).start()

                result_queue.put({"type": "reset", "trigger": text})
                report_to_server('/api/agent/reset', {
                    'agent_name': session_info.get('agent_name', ''),
                })
                print(f"{ts()} ⏳ Wacht op begroeting voor volgend gesprek...")

        except Exception as e:
            if listen_active.is_set():
                print(f"{ts()} ❌ Onverwachte fout: {e}")

    # Sessie gestopt — als er nog een transcript is, analyseer dat ook
    if _call_transcript and _call_active:
        print(f"{ts()} 📄 Sessie gestopt met {len(_call_transcript)} zinnen — analyseer...")
        transcript_copy = list(_call_transcript)
        _call_transcript.clear()
        _call_active = False
        threading.Thread(
            target=_process_post_call,
            args=(transcript_copy,),
            daemon=True
        ).start()

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
    # Stop any running loop first
    listen_active.clear()
    if _listen_thread and _listen_thread.is_alive():
        _listen_thread.join(timeout=3)
    recent_sentences.clear()
    listen_active.set()
    _listen_thread = threading.Thread(target=listen_loop, daemon=True)
    _listen_thread.start()
    report_to_server('/api/agent/online', {'agent_name': agent_name, 'role': role})
    print(f"{ts()} ▶️  Sessie gestart — {role} · {agent_name}")
    return jsonify({'status': 'started'})


@app.route('/stop', methods=['POST'])
def stop():
    listen_active.clear()
    agent_name = session_info.get('agent_name', '')
    report_to_server('/api/agent/offline', {'agent_name': agent_name})
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
                elif data.get('type') == 'post_call':
                    yield f"event: post_call\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                elif data.get('type') == 'call_start':
                    yield f"event: call_start\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(
        stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()
    threading.Timer(1.0, lambda: webbrowser.open('http://localhost:5000')).start()
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
