import os
import sys
import json
import tempfile
import smtplib
import queue
import threading
import webbrowser
import logging
import logging.handlers
import time as _time
from datetime import datetime
from collections import deque

# ── Pad-setup ──
_INSTALL_DIR = os.path.join(os.environ.get("APPDATA", ""), "NVL-Compliance")
_ENV_PATH = os.path.join(_INSTALL_DIR, ".env")
os.makedirs(_INSTALL_DIR, exist_ok=True)

# ── Logging naar bestand (altijd, ook windowed mode) ──
_LOG_PATH = os.path.join(_INSTALL_DIR, "compliance.log")

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
log = logging.getLogger("compliance")
log.info("=== NVL Compliance start ===")

# ── Installer GUI (draait VOOR alle zware imports) ──
def _run_installer():
    """Tkinter install-wizard. Slaat .env op en herstart de app."""
    import tkinter as tk
    import winreg

    CENTRAL_SERVER = "http://172.28.1.57:8000"
    VERSION = "1.0.0"

    def do_install(name, role, groq_key, cerebras_key, status_label, progress_lbl, root):
        if not name or not groq_key or not cerebras_key:
            status_label.config(text="Vul alle velden in", fg="#FF1744")
            return
        status_label.config(text="Installeren...", fg="#FFD600")
        progress_lbl.config(text="[1/4] Config opslaan...")
        root.update()

        os.makedirs(_INSTALL_DIR, exist_ok=True)
        with open(_ENV_PATH, "w", encoding="utf-8") as f:
            f.write(
                f"CENTRAL_SERVER={CENTRAL_SERVER}\n"
                f"AGENT_KEY=NVL2026\n"
                f"GROQ_API_KEY={groq_key}\n"
                f"CEREBRAS_API_KEY={cerebras_key}\n"
                f"AGENT_NAME={name}\n"
                f"AGENT_ROLE={role}\n"
            )

        # [2/4] Backend connection test
        progress_lbl.config(text="[2/4] Backend verbinding testen...")
        root.update()
        try:
            import urllib.request
            req = urllib.request.Request(f"{CENTRAL_SERVER}/api/status",
                                        headers={"X-Admin-Key": "Voltera"})
            urllib.request.urlopen(req, timeout=5)
            progress_lbl.config(text="[2/4] Backend bereikbaar!")
        except Exception:
            progress_lbl.config(text="[2/4] Backend niet bereikbaar (niet kritiek)")

        root.update()

        # [3/4] Autostart
        progress_lbl.config(text="[3/4] Autostart instellen...")
        root.update()
        exe_path = sys.executable
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "NVL-Compliance", 0, winreg.REG_SZ, exe_path)
            winreg.CloseKey(key)
        except Exception:
            pass

        # [4/4] Desktop snelkoppeling
        progress_lbl.config(text="[4/4] Desktop snelkoppeling...")
        root.update()
        try:
            from win32com.client import Dispatch
            desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
            shortcut = Dispatch("WScript.Shell").CreateShortCut(
                os.path.join(desktop, "NVL Compliance.lnk"))
            shortcut.Targetpath = exe_path
            shortcut.WorkingDirectory = os.path.dirname(exe_path)
            shortcut.Description = "NVL Compliance Checker"
            shortcut.save()
        except Exception:
            pass

        status_label.config(text="Installatie voltooid - app start...", fg="#00C853")
        progress_lbl.config(text="Klaar!")
        root.update()
        root.after(1000, root.destroy)

    root = tk.Tk()
    root.title("NVL Compliance - Installatie")
    root.geometry("460x560")
    root.resizable(False, False)
    root.configure(bg="#0D0D0D")

    tk.Label(root, text="NVL Compliance Checker",
             font=("Segoe UI", 16, "bold"), bg="#0D0D0D", fg="white").pack(pady=(30, 4))
    tk.Label(root, text=f"Eenmalige installatie  v{VERSION}",
             font=("Segoe UI", 10), bg="#0D0D0D", fg="#666").pack(pady=(0, 24))

    frame = tk.Frame(root, bg="#0D0D0D")
    frame.pack(padx=40, fill="x")

    def make_field(label_text, show=None):
        tk.Label(frame, text=label_text, font=("Segoe UI", 9),
                 bg="#0D0D0D", fg="#888", anchor="w").pack(fill="x", pady=(8, 2))
        e = tk.Entry(frame, font=("Segoe UI", 11), bg="#1E1E1E", fg="white",
                     insertbackground="white", relief="flat", bd=8, show=show)
        e.pack(fill="x", ipady=4)
        return e

    name_entry = make_field("Jouw naam")

    tk.Label(frame, text="Jouw rol", font=("Segoe UI", 9),
             bg="#0D0D0D", fg="#888", anchor="w").pack(fill="x", pady=(8, 2))
    role_var = tk.StringVar(value="nvl")
    role_frame = tk.Frame(frame, bg="#0D0D0D")
    role_frame.pack(fill="x")
    for val, label in [("nvl", "NVL Planner"), ("voltera", "Voltera Closer")]:
        tk.Radiobutton(role_frame, text=label, variable=role_var, value=val,
                       bg="#0D0D0D", fg="white", selectcolor="#1E1E1E",
                       font=("Segoe UI", 10), activebackground="#0D0D0D",
                       activeforeground="white").pack(side="left", padx=(0, 16))

    groq_entry = make_field("Groq API Key")
    cerebras_entry = make_field("Cerebras API Key")

    status_label = tk.Label(root, text="", font=("Segoe UI", 9),
                            bg="#0D0D0D", fg="#00C853")
    status_label.pack(pady=(12, 0))

    progress_lbl = tk.Label(root, text="", font=("Segoe UI", 9),
                            bg="#0D0D0D", fg="#888")
    progress_lbl.pack(pady=(4, 0))

    tk.Button(root, text="Installeren en starten",
              font=("Segoe UI", 11, "bold"), bg="#2979FF", fg="white",
              relief="flat", bd=0, padx=20, pady=10, cursor="hand2",
              command=lambda: do_install(
                  name_entry.get().strip(), role_var.get(),
                  groq_entry.get().strip(), cerebras_entry.get().strip(),
                  status_label, progress_lbl, root
              )).pack(pady=20)

    root.mainloop()


# ── Install check: MOET voor alle zware imports ──
if not os.path.exists(_ENV_PATH):
    if not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        _run_installer()
        if not os.path.exists(_ENV_PATH):
            sys.exit(0)  # User sloot installer zonder te installeren

# ── Nu pas de zware imports ──
from dotenv import load_dotenv
if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()

import numpy as np
import sounddevice as sd
import soundfile as sf
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests as http_requests
from flask import Flask, Response, request, jsonify, send_from_directory
from groq import Groq
from cerebras.cloud.sdk import Cerebras

# Pre-configured agent (vanuit installer)
AGENT_NAME = os.getenv("AGENT_NAME", "")
AGENT_ROLE = os.getenv("AGENT_ROLE", "")

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
cerebras_client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))

def _mask_key(key: str | None) -> str:
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "..." + key[-4:]

log.info("Groq key: %s", _mask_key(os.getenv("GROQ_API_KEY")))
log.info("Cerebras key: %s", _mask_key(os.getenv("CEREBRAS_API_KEY")))
log.info("Groq Whisper + Cerebras LLM klaar")


# ── Retry helper ──────────────────────────────────────────────────────────────
def _retry(fn, max_retries=3, backoff=2.0, label="API call"):
    """Retry een functie met exponential backoff. Gooit originele fout na max_retries."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = backoff ** (attempt - 1)
                log.warning("%s mislukt (poging %d/%d): %s — retry in %.1fs",
                            label, attempt, max_retries, e, wait)
                _time.sleep(wait)
            else:
                log.error("%s mislukt na %d pogingen: %s", label, max_retries, e)
    raise last_err

# ── Microfoon diagnostiek bij startup ──
def _probe_microphone():
    """Test of er een werkende microfoon is. Geeft (device_index, device_name) of (None, error)."""
    try:
        import sounddevice as _sd
        devices = _sd.query_devices()
        default_in = _sd.default.device[0] if isinstance(_sd.default.device, (list, tuple)) else _sd.default.device
        log.info(f"Beschikbare audio devices:")
        mic_found = False
        for i, d in enumerate(devices):
            if d['max_input_channels'] > 0:
                marker = " <-- DEFAULT" if i == default_in else ""
                log.info(f"  [{i}] {d['name']} (inputs={d['max_input_channels']}){marker}")
                mic_found = True
        if not mic_found:
            log.error("GEEN input devices gevonden!")
            return None, "Geen microfoon gevonden op dit systeem"
        # Test opname
        test = _sd.rec(int(16000 * 0.5), samplerate=16000, channels=1, dtype='float32')
        _sd.wait()
        rms = float((test ** 2).mean() ** 0.5)
        log.info(f"Microfoon test OK (device={default_in}, rms={rms:.6f})")
        return default_in, devices[default_in]['name']
    except Exception as e:
        log.error(f"Microfoon test MISLUKT: {e}")
        return None, str(e)

_mic_device, _mic_info = _probe_microphone()
log.info(f"Microfoon resultaat: device={_mic_device}, info={_mic_info}")

CENTRAL_SERVER = os.getenv("CENTRAL_SERVER", "http://localhost:8000")
AGENT_KEY = os.getenv("AGENT_KEY", "NVL2026")

app = Flask(__name__)

result_queue: queue.Queue = queue.Queue()
listen_active = threading.Event()
_listen_thread: threading.Thread | None = None
session_info: dict = {}


def report_to_server(endpoint: str, data: dict) -> None:
    """Stuurt data naar de centrale server. Bij fout: retry met backoff."""
    def _do():
        http_requests.post(
            f"{CENTRAL_SERVER}{endpoint}",
            json=data,
            headers={"X-Agent-Key": AGENT_KEY},
            timeout=5,
        )
    try:
        _retry(_do, max_retries=3, backoff=2.0, label=f"server{endpoint}")
    except Exception as e:
        log.warning("Centrale server niet bereikbaar (%s): %s", endpoint, e)


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


def record_block() -> np.ndarray | None:
    """Neemt een blok van BLOCK_SECONDS seconden op en geeft float32 array terug."""
    try:
        samples = int(SAMPLE_RATE * BLOCK_SECONDS)
        audio = sd.rec(samples, samplerate=SAMPLE_RATE, channels=CHANNELS,
                       dtype='float32')
        sd.wait()
        return audio.flatten()
    except Exception as e:
        log.error(f"Opname mislukt: {e}")
        return None


def _recorder_loop() -> None:
    """Recorder thread: neemt continu blokken op en plaatst ze in de audio queue."""
    global _overlap_buffer
    _fail_count = 0
    while listen_active.is_set():
        new_block = record_block()
        if new_block is None:
            _fail_count += 1
            log.error(f"record_block mislukt (poging {_fail_count})")
            if _fail_count >= 5:
                log.error("Te veel opname fouten — recorder stopt")
                result_queue.put({"type": "error", "message": "Microfoon werkt niet. Controleer audio-instellingen."})
                return
            import time; time.sleep(1)
            continue
        _fail_count = 0
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
    """Transcribeert één audio blok via Groq Whisper, met retry."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        sf.write(tmp_path, audio_block, SAMPLE_RATE)

        def _do_transcribe():
            with open(tmp_path, "rb") as audio_file:
                response = groq_client.audio.transcriptions.create(
                    file=("audio.wav", audio_file, "audio/wav"),
                    model="whisper-large-v3-turbo",
                    language="nl",
                    response_format="text",
                )
            return response

        response = _retry(_do_transcribe, max_retries=2, backoff=1.5, label="Whisper")
        text = response.strip() if isinstance(response, str) else str(response).strip()
        return text if text else None
    except Exception as e:
        log.error("Transcriptie mislukt: %s", e)
        return None
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
    """Post-call analyse: stuurt het hele transcript naar qwen-235B met retry."""
    if not transcript:
        return []

    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(transcript))

    def _do_analyse():
        return cerebras_client.chat.completions.create(
            model="qwen-3-235b-a22b-instruct-2507",
            messages=[
                {"role": "system", "content": POST_CALL_PROMPT},
                {"role": "user", "content": numbered},
            ],
            response_format={"type": "json_object"},
        )

    try:
        response = _retry(_do_analyse, max_retries=2, backoff=3.0, label="Cerebras post-call")
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
        log.error("Post-call analyse fout: %s", e)
        return []


def _process_post_call(transcript: list[str]) -> None:
    """Draait post-call analyse in een aparte thread zodat de main loop niet blokkeert."""
    log.info("Post-call analyse gestart (%d zinnen)", len(transcript))
    violations = analyse_full_transcript(transcript)

    if not violations:
        log.info("Geen overtredingen in dit gesprek")
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

    log.info("%d overtreding(en) gevonden", len(violations))
    for v in violations:
        sev = v.get("severity", "?")
        log.info("  [%s] %s | Quote: %s", sev.upper(), v.get('rule_violated', '?'), v.get('problematic_quote', ''))

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
    log.info("Post-call analyse compleet")


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
        log.warning("BACKOFFICE_EMAIL niet geconfigureerd in .env")
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
                   "Compliance overtreding gedetecteerd", body)
        log.info("Backoffice alert verstuurd naar %s", backoffice_email)
    except Exception as e:
        log.error("E-mail naar backoffice mislukt: %s", e)


def listen_loop() -> None:
    log.info("Compliance BOT luistert - hybride modus")

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

    print(f"{ts()} Wacht op begroeting om gesprek te starten...")

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
                log.debug("Whisper hallucinatie gefilterd")
                continue

            # ── INSTANT ALERT: altijd actief, ook buiten gesprek ──
            instant_match = next((kw for kw in INSTANT_CRITICAL_KEYWORDS if kw in text_lower), None)
            if instant_match:
                log.info("INSTANT CRITICAL: '%s' in: %s", instant_match, text)
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
                    print(f"\n{ts()} Nieuw gesprek gedetecteerd: '{text}'")
                    result_queue.put({"type": "call_start", "trigger": text})
                    result_queue.put({"spoken": text, "result": {
                        "violation": False, "severity": None, "confidence": 1.0,
                        "rule_violated": None, "problematic_quote": None, "explanation": None,
                    }})
                else:
                    # Geen actief gesprek en geen begroeting → skip
                    print(f"{ts()} Buiten gesprek: {text[:50]}...")
                continue

            # ── ACTIEF GESPREK: verzamel transcript ──
            _call_transcript.append(text)
            print(f"{ts()} [{len(_call_transcript)}] {text}")
            result_queue.put({"spoken": text, "result": {
                "violation": False, "severity": None, "confidence": 1.0,
                "rule_violated": None, "problematic_quote": None, "explanation": None,
            }})

            # ── CLOSING PHRASE → trigger post-call analyse ──
            closing_match = next((p for p in CLOSING_PHRASES if p in text_lower), None)
            if closing_match:
                print(f"{ts()} Gespreksafsluiting gedetecteerd: '{text}'")
                print(f"{ts()} Transcript bevat {len(_call_transcript)} zinnen")

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
                print(f"{ts()} Wacht op begroeting voor volgend gesprek...")

        except Exception as e:
            if listen_active.is_set():
                print(f"{ts()} Onverwachte fout: {e}")

    # Sessie gestopt — als er nog een transcript is, analyseer dat ook
    if _call_transcript and _call_active:
        print(f"{ts()} Sessie gestopt met {len(_call_transcript)} zinnen -- analyseer...")
        transcript_copy = list(_call_transcript)
        _call_transcript.clear()
        _call_active = False
        threading.Thread(
            target=_process_post_call,
            args=(transcript_copy,),
            daemon=True
        ).start()

    print(f"{ts()} Luister loop gestopt.")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # PyInstaller bundled path support
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return send_from_directory(base, 'compliance-ui.html')


@app.route('/config')
def config():
    """Geeft pre-configured agent info terug (vanuit installer)."""
    return jsonify({
        'agent_name': AGENT_NAME,
        'role': AGENT_ROLE,
        'preconfigured': bool(AGENT_NAME and AGENT_ROLE),
    })


@app.route('/health')
def health():
    """Health check: test ook of de centrale server bereikbaar is."""
    backend_ok = False
    try:
        r = http_requests.get(f"{CENTRAL_SERVER}/api/status",
                              headers={"X-Admin-Key": "Voltera"}, timeout=3)
        backend_ok = r.status_code == 200
    except Exception:
        pass
    return jsonify({
        'status': 'ok',
        'backend_connected': backend_ok,
        'central_server': CENTRAL_SERVER,
        'mic_ok': _mic_device is not None,
    })


@app.route('/devices')
def devices():
    """Geeft beschikbare audio input devices terug."""
    try:
        devs = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        inputs = []
        for i, d in enumerate(devs):
            if d['max_input_channels'] > 0:
                inputs.append({
                    'index': i,
                    'name': d['name'],
                    'channels': d['max_input_channels'],
                    'default': i == default_in,
                })
        return jsonify({
            'devices': inputs,
            'mic_ok': _mic_device is not None,
            'mic_info': _mic_info,
            'log_path': _LOG_PATH,
        })
    except Exception as e:
        return jsonify({'devices': [], 'mic_ok': False, 'mic_info': str(e)})


@app.route('/log')
def show_log():
    """Toont de laatste 50 regels van het logbestand."""
    try:
        with open(_LOG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return '<pre>' + ''.join(lines[-50:]) + '</pre>'
    except Exception as e:
        return f'<pre>Log niet beschikbaar: {e}</pre>'


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
    log.info("Sessie gestart: %s (%s)", agent_name, role)
    return jsonify({'status': 'started'})


@app.route('/stop', methods=['POST'])
def stop():
    listen_active.clear()
    agent_name = session_info.get('agent_name', '')
    report_to_server('/api/agent/offline', {'agent_name': agent_name})
    log.info("Sessie gestopt")
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
                elif data.get('type') == 'error':
                    yield f"event: error_msg\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
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
