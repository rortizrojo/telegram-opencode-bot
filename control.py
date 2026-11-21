import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import messagebox
import threading

WORKDIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(WORKDIR, "bot.py")
SHIM_SCRIPT = os.path.join(WORKDIR, "oc_shim.py")


def _python_exe() -> str:
    """python.exe (no pythonw) para que el shim tenga stdout donde loguear."""
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


PYTHON = _python_exe()
PID_FILE = os.path.join(WORKDIR, "bot.pid")
ENV_FILE = os.path.join(WORKDIR, ".env")
SHIM_HEALTH = "http://127.0.0.1:4095/health"


def _leer_env() -> dict:
    datos = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                clave, valor = linea.split("=", 1)
                datos[clave.strip()] = valor.strip().strip('"').strip("'")
    except OSError:
        pass
    return datos


def _env_para_hijos() -> dict:
    """El .env manda tambien para el motor (OC_ENGINE, OC_DIR, password...)."""
    env = dict(os.environ)
    datos = _leer_env()
    env.update(datos)
    if datos.get("OPENCODE_PASS"):
        env["OC_V2_PASS"] = datos["OPENCODE_PASS"]
    return env

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW

# ---------------------------------------------------------------- configuracion
_CFG = _leer_env()
MODO = (_CFG.get("MODO") or "v2").strip().lower()            # "v2" (motor propio) o "v1" (motor externo/WSL)
OPENCODE_URL = (_CFG.get("OPENCODE_URL") or "http://127.0.0.1:4095").rstrip("/")
WSL_DISTRO = (_CFG.get("WSL_DISTRO") or "").strip()
WSL_CMD = (_CFG.get("WSL_CMD") or "").strip()

btn_start = None
btn_stop = None
btn_restart = None
status_label = None
shim_label = None
_starting = threading.Lock()


def _process_exists(pid: int) -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return str(pid) in r.stdout


def _scan_pids(keyword: str) -> list[int]:
    """PIDs reales de python.exe cuyo script contiene `keyword`."""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*" + keyword + "*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=25,
        )
    except Exception:
        return []
    pids = []
    for line in r.stdout.split():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def is_bot_running() -> list[int]:
    if not os.path.exists(PID_FILE):
        return []
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        if _process_exists(pid):
            return [pid]
        else:
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            return []
    except (ValueError, OSError):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return []


def clean_stale_pid():
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        if not _process_exists(pid):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
    except (ValueError, OSError):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def shim_alive() -> bool:
    try:
        with urllib.request.urlopen(SHIM_HEALTH, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def motor_externo_arriba() -> bool:
    """Comprueba el motor v1 (propio o en WSL) por HTTP: 401 tambien cuenta como arriba."""
    try:
        req = urllib.request.Request(OPENCODE_URL + "/session")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status in (200, 401, 403)
        except urllib.error.HTTPError as e:
            return e.code in (401, 403)
    except Exception:
        return False


def motor_arriba() -> bool:
    return motor_externo_arriba() if MODO == "v1" else shim_alive()


def _arrancar_en_wsl() -> bool:
    """Lanza el motor dentro de WSL en segundo plano (si esta configurado)."""
    if not WSL_CMD:
        return False
    cmd = (
        "setsid nohup " + WSL_CMD +
        " >/tmp/opencode-wsl.log 2>&1 </dev/null & disown; echo lanzado"
    )
    args = ["wsl.exe"]
    if WSL_DISTRO:
        args += ["-d", WSL_DISTRO]
    args += ["-e", "bash", "-lc", cmd]
    try:
        subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def ensure_shim(timeout: int = 45) -> bool:
    """Deja el motor disponible: shim propio (v2) o motor externo/WSL (v1)."""
    if motor_arriba():
        return True
    if MODO == "v1":
        if not _arrancar_en_wsl():
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            if motor_arriba():
                return True
        return False
    if not os.path.exists(SHIM_SCRIPT):
        return False
    subprocess.Popen(
        [PYTHON, SHIM_SCRIPT],
        cwd=WORKDIR,
        env=_env_para_hijos(),
        creationflags=CREATE_NO_WINDOW,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        if shim_alive():
            return True
    return False


def stop_shim():
    """Para el shim y su motor v2 (arbol de procesos). En modo v1 no toca nada externo."""
    if MODO == "v1":
        return
    for pid in _scan_pids("oc_shim.py"):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )


def update_buttons():
    clean_stale_pid()
    pids = is_bot_running()
    running = len(pids) > 0
    color = "green" if running else "red"
    text = f"RUNNING ({', '.join(str(p) for p in pids)})" if running else "STOPPED"
    status_label.config(text=text, fg=color)

    if MODO == "v1":
        arriba = motor_externo_arriba()
        etiqueta = "motor v1 (WSL): " + ("OK" if arriba else "parado")
    else:
        arriba = shim_alive()
        etiqueta = "motor v2: " + ("OK" if arriba else "parado")
    shim_label.config(text=etiqueta, fg="green" if arriba else "#d08b00")

    btn_start.config(state=tk.DISABLED if running else tk.NORMAL)
    btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)
    btn_restart.config(state=tk.NORMAL if running else tk.DISABLED)


def start_bot():
    if not _starting.acquire(blocking=False):
        return
    try:
        running = _scan_pids("bot.py") or is_bot_running()
        if running:
            if len(running) > 1:
                for pid in running[1:]:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                messagebox.showinfo(
                    "Info",
                    "Habia varias instancias del bot; dejo una y cierro el resto.",
                )
            else:
                messagebox.showinfo("Info", "El bot ya esta en ejecucion")
            update_buttons()
            return

        if not ensure_shim():
            if MODO == "v1":
                detalle = (
                    "No encuentro el motor en " + OPENCODE_URL + ".\n\n"
                    "Arrancalo dentro de WSL con:\n"
                    "  opencode serve --port 4096 --hostname 0.0.0.0\n\n"
                    "O rellena WSL_CMD en .env para que el panel lo arranque solo."
                )
            else:
                detalle = (
                    "No pude arrancar el motor v2 (oc_shim.py).\n"
                    "Revisa oc_shim.log en la carpeta del bot."
                )
            messagebox.showerror("Error", detalle)
            update_buttons()
            return

        subprocess.Popen(
            [PYTHON, BOT_SCRIPT],
            cwd=WORKDIR,
            env=_env_para_hijos(),
            creationflags=CREATE_NO_WINDOW,
        )
        # esperar a que el bot escriba su PID para evitar dobles arranques
        for _ in range(20):
            time.sleep(0.5)
            if is_bot_running():
                break
    finally:
        _starting.release()
        update_buttons()


def stop_bot():
    pids = _scan_pids("bot.py") or is_bot_running()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    time.sleep(1)
    stop_shim()
    update_buttons()


def restart_bot():
    stop_bot()
    root.after(2000, start_bot)


def poll_status():
    update_buttons()
    root.after(2000, poll_status)


root = tk.Tk()
root.title("Telegram Bot - opencode")
root.geometry("380x215")
root.resizable(False, False)
root.configure(bg="#2b2b2b")

tk.Label(root, text="Telegram Bot", font=("Segoe UI", 14, "bold"), bg="#2b2b2b", fg="white").pack(pady=(15, 2))

status_label = tk.Label(root, text="...", font=("Segoe UI", 11, "bold"), bg="#2b2b2b", fg="red")
status_label.pack(pady=(0, 2))

shim_label = tk.Label(root, text="motor v2: ...", font=("Segoe UI", 9), bg="#2b2b2b", fg="#888888")
shim_label.pack(pady=(0, 12))

frame = tk.Frame(root, bg="#2b2b2b")
frame.pack()

btn_style = {"font": ("Segoe UI", 10), "width": 9, "bd": 0, "cursor": "hand2"}

btn_start = tk.Button(frame, text="Arrancar", command=start_bot, bg="#4CAF50", fg="white", **btn_style)
btn_start.grid(row=0, column=0, padx=5)

btn_stop = tk.Button(frame, text="Parar", command=stop_bot, bg="#f44336", fg="white", **btn_style)
btn_stop.grid(row=0, column=1, padx=5)

btn_restart = tk.Button(frame, text="Reiniciar", command=restart_bot, bg="#FF9800", fg="white", **btn_style)
btn_restart.grid(row=0, column=2, padx=5)

poll_status()
root.mainloop()
