import tkinter as tk
import os
import sys
import subprocess
import winreg

CENTRAL_SERVER = "http://172.28.1.57:8000"
INSTALL_DIR = os.path.join(os.environ["APPDATA"], "NVL-Compliance")
ENV_FILE = os.path.join(INSTALL_DIR, ".env")
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_installed():
    return os.path.exists(ENV_FILE)


def install(agent_name, role, groq_key, cerebras_key, status_label, root):
    status_label.config(text="Installeren...", fg="#FFD600")
    root.update()

    os.makedirs(INSTALL_DIR, exist_ok=True)

    env_content = (
        f"CENTRAL_SERVER={CENTRAL_SERVER}\n"
        f"AGENT_KEY=NVL2026\n"
        f"GROQ_API_KEY={groq_key}\n"
        f"CEREBRAS_API_KEY={cerebras_key}\n"
        f"AGENT_NAME={agent_name}\n"
        f"AGENT_ROLE={role}\n"
    )
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(env_content)

    # Autostart bij Windows login
    exe_path = sys.executable
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            STARTUP_KEY, 0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "NVL-Compliance", 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Autostart instellen mislukt: {e}")

    # Desktop snelkoppeling
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    shortcut_path = os.path.join(desktop, "NVL Compliance.lnk")
    try:
        from win32com.client import Dispatch
        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(shortcut_path)
        shortcut.Targetpath = exe_path
        shortcut.WorkingDirectory = INSTALL_DIR
        shortcut.Description = "NVL Compliance Checker"
        shortcut.save()
    except Exception as e:
        print(f"Snelkoppeling maken mislukt: {e}")

    status_label.config(text="✅ Installatie voltooid — app start...", fg="#00C853")
    root.update()
    root.after(1000, root.destroy)


def show_installer():
    root = tk.Tk()
    root.title("NVL Compliance — Installatie")
    root.geometry("460x520")
    root.resizable(False, False)
    root.configure(bg="#0D0D0D")

    tk.Label(
        root, text="NVL Compliance Checker",
        font=("Segoe UI", 16, "bold"),
        bg="#0D0D0D", fg="white",
    ).pack(pady=(30, 4))

    tk.Label(
        root, text="Eenmalige installatie",
        font=("Segoe UI", 10),
        bg="#0D0D0D", fg="#666",
    ).pack(pady=(0, 24))

    frame = tk.Frame(root, bg="#0D0D0D")
    frame.pack(padx=40, fill="x")

    def field(label_text, show=None):
        tk.Label(
            frame, text=label_text,
            font=("Segoe UI", 9),
            bg="#0D0D0D", fg="#888",
            anchor="w",
        ).pack(fill="x", pady=(8, 2))
        entry = tk.Entry(
            frame,
            font=("Segoe UI", 11),
            bg="#1E1E1E", fg="white",
            insertbackground="white",
            relief="flat", bd=8,
            show=show,
        )
        entry.pack(fill="x", ipady=4)
        return entry

    name_entry = field("Jouw naam")

    # Rol selectie
    tk.Label(
        frame, text="Jouw rol",
        font=("Segoe UI", 9),
        bg="#0D0D0D", fg="#888",
        anchor="w",
    ).pack(fill="x", pady=(8, 2))

    role_var = tk.StringVar(value="nvl")
    role_frame = tk.Frame(frame, bg="#0D0D0D")
    role_frame.pack(fill="x")

    for val, label in [("nvl", "NVL Planner"), ("voltera", "Voltera Closer")]:
        tk.Radiobutton(
            role_frame,
            text=label, variable=role_var, value=val,
            bg="#0D0D0D", fg="white",
            selectcolor="#1E1E1E",
            font=("Segoe UI", 10),
            activebackground="#0D0D0D",
            activeforeground="white",
        ).pack(side="left", padx=(0, 16))

    groq_entry = field("Groq API Key")
    cerebras_entry = field("Cerebras API Key")

    status_label = tk.Label(
        root, text="",
        font=("Segoe UI", 9),
        bg="#0D0D0D", fg="#00C853",
    )
    status_label.pack(pady=(12, 0))

    def on_install():
        name = name_entry.get().strip()
        groq_key = groq_entry.get().strip()
        cerebras_key = cerebras_entry.get().strip()
        if not name or not groq_key or not cerebras_key:
            status_label.config(text="Vul alle velden in", fg="#FF1744")
            return
        install(name, role_var.get(), groq_key, cerebras_key, status_label, root)

    tk.Button(
        root,
        text="Installeren en starten",
        font=("Segoe UI", 11, "bold"),
        bg="#2979FF", fg="white",
        relief="flat", bd=0,
        padx=20, pady=10,
        cursor="hand2",
        command=on_install,
    ).pack(pady=20)

    root.mainloop()


if __name__ == "__main__":
    show_installer()
