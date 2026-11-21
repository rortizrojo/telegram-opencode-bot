"""Shim v1 -> v2 para el bot de Telegram.

El bot habla la API v1 de opencode (/session, /session/{id}/message, /question...).
El motor v2 del escritorio expone /api/*. Este proceso:

  * emula la API v1 en 127.0.0.1:4095,
  * traduce cada llamada a la API v2,
  * convierte los mensajes v2 (content[]) al formato v1 (info + parts[]),
  * arranca y vigila su propio motor v2 (opencode-cli.exe serve) en 127.0.0.1:4098.

Variables de entorno:
  OC_V2_URL     base del motor v2          (def. http://127.0.0.1:4098)
  OC_V2_PASS    password del motor v2      (def. opencode123)
  OC_SHIM_PORT  puerto del shim            (def. 4095)
  OC_ENGINE     ruta de opencode-cli.exe   (def. la del escritorio 2.2.2)
  OC_DIR        directorio por defecto de sesiones nuevas
  OC_NO_ENGINE  =1 para no gestionar el motor (usar uno externo)
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from base64 import b64encode

from aiohttp import ClientSession, ClientTimeout, web

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oc_shim.log")
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oc_shim.pid")

V2_URL = os.environ.get("OC_V2_URL", "http://127.0.0.1:4098").rstrip("/")
V2_PASS = os.environ.get("OC_V2_PASS", "opencode123")
V2_PORT = int(os.environ.get("OC_V2_PORT", "4098"))
SHIM_PORT = int(os.environ.get("OC_SHIM_PORT", "4095"))
def _detect_engine() -> str | None:
    """Busca opencode-cli.exe (motor v2) en los sitios habituales.

    OC_ENGINE manda si esta definido y existe. Si no, se escanean las rutas
    tipicas y se elige la version mas nueva, para que actualizar la app no
    rompa el arranque.
    """
    explicito = os.environ.get("OC_ENGINE")
    if explicito and os.path.exists(explicito):
        return explicito

    home = os.path.expanduser("~")
    candidatos: list[str] = []

    cli_root = os.path.join(home, "AppData", "Roaming", "ai.opencode.desktop", "cli")
    if os.path.isdir(cli_root):
        for nombre in os.listdir(cli_root):
            p = os.path.join(cli_root, nombre, "opencode-cli.exe")
            if os.path.exists(p):
                candidatos.append(p)
    candidatos.append(
        os.path.join(home, "AppData", "Local", "Programs", "@opencode-aidesktop",
                     "resources", "opencode-cli.exe")
    )
    candidatos.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "opencode-cli.exe"))
    en_path = shutil.which("opencode-cli")
    if en_path:
        candidatos.append(en_path)

    existentes = [c for c in candidatos if os.path.exists(c)]
    if not existentes:
        return None

    def clave(p: str):
        # version por nombre de carpeta (2.2.10 > 2.2.9) y, si no, por fecha
        partes = os.path.basename(os.path.dirname(p)).split(".")[:4]
        nums = []
        for x in partes:
            if x.isdigit():
                nums.append(int(x))
            else:
                break
        return (tuple(nums), os.path.getmtime(p))

    existentes.sort(key=clave, reverse=True)
    return existentes[0]


ENGINE_EXE = _detect_engine()
_DIR_POR_DEFECTO = os.path.join(os.path.expanduser("~"), "OneDrive", "Documentos", "Default Project")
DEFAULT_DIR = os.environ.get("OC_DIR") or (
    _DIR_POR_DEFECTO if os.path.isdir(_DIR_POR_DEFECTO) else os.path.expanduser("~")
)
MANAGE_ENGINE = os.environ.get("OC_NO_ENGINE", "") not in ("1", "true", "yes")

AUTH = {"Authorization": "Basic " + b64encode(f"opencode:{V2_PASS}".encode()).decode()}

TOOL_ALIASES = {
    "shell": "bash",
    "patch": "edit",
    "subagent": "task",
    "list": "ls",
}

# sessions prompts sent through the shim: sid -> timestamp
_recent_prompts: dict[str, float] = {}
# forms seen per session: sid -> list[form]
_forms_cache: dict[str, list[dict]] = {}
# permission id -> (sessionID, raw)
_permissions_cache: dict[str, str] = {}
_active_cache = {"at": 0.0, "data": set()}

log = logging.getLogger("oc_shim")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


# ---------------------------------------------------------------- v2 client

async def v2(method: str, path: str, body=None, timeout=60.0):
    """Llamada al motor v2. Devuelve (status, payload)."""
    url = V2_URL + path
    tmo = ClientTimeout(total=timeout)
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(AUTH)
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with ClientSession(timeout=tmo, headers=headers) as s:
            async with s.request(method, url, data=data) as r:
                raw = await r.read()
                if not raw:
                    return r.status, None
                try:
                    return r.status, json.loads(raw)
                except Exception:
                    return r.status, raw.decode("utf-8", "replace")
    except Exception as e:
        log.warning("v2 %s %s fallo: %s", method, path, e)
        return 0, {"error": str(e)}


# ---------------------------------------------------------------- mapping

def _tool_name(name: str) -> str:
    return TOOL_ALIASES.get(name or "", name or "?")


def _tool_state(item: dict) -> dict:
    st = item.get("state") or {}
    inp = st.get("input") or {}
    if isinstance(inp, dict) and "path" in inp and "filePath" not in inp:
        inp = dict(inp)
        inp["filePath"] = inp["path"]
    out = ""
    content = st.get("content")
    if isinstance(content, list):
        out = "".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    if not out and isinstance(st.get("output"), str):
        out = st["output"]
    status = st.get("status") or "running"
    return {
        "status": status,
        "input": inp,
        "output": out,
        "metadata": st.get("metadata") or {},
        "title": st.get("title") or "",
    }


def _map_error(err) -> dict | None:
    if not err:
        return None
    if not isinstance(err, dict):
        return {"name": "APIError", "data": {"message": str(err)}}
    etype = str(err.get("type") or "")
    msg = str(err.get("message") or "")
    low = (etype + " " + msg).lower()
    if "abort" in low or "interrupt" in low or "cancel" in low:
        name = "MessageAbortedError"
    elif err.get("status") or err.get("response"):
        name = "APIError"
    else:
        name = etype or "APIError"
    return {"name": name, "data": {"message": msg or etype or "error"}}


def _map_parts(msg: dict) -> list[dict]:
    parts = []
    for c in msg.get("content") or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        if ctype == "text":
            if c.get("text"):
                parts.append({"type": "text", "text": c.get("text")})
        elif ctype == "reasoning":
            if c.get("text"):
                parts.append({"type": "reasoning", "text": c.get("text")})
        elif ctype == "tool":
            parts.append({
                "type": "tool",
                "tool": _tool_name(c.get("name")),
                "callID": c.get("id") or "",
                "state": _tool_state(c),
            })
    return parts


def _map_message(msg: dict) -> dict | None:
    mtype = msg.get("type")
    mid = msg.get("id") or ""
    time_info = {"created": (msg.get("time") or {}).get("created")}
    if (msg.get("time") or {}).get("completed"):
        time_info["completed"] = msg["time"]["completed"]

    if mtype == "user":
        # v2 guarda el texto en "text"; al crearlo por /prompt viene en "payload.text"
        text = msg.get("text") or ""
        payload = msg.get("payload") or {}
        if not text and isinstance(payload, dict):
            text = payload.get("text") or ""
        if not text and isinstance(payload, str):
            text = payload
        parts = [{"type": "text", "text": text}] if text else []
        return {"info": {"id": mid, "role": "user", "sessionID": msg.get("sessionID"),
                         "time": time_info, "finish": None}, "parts": parts}

    if mtype == "assistant":
        info = {
            "id": mid,
            "role": "assistant",
            "sessionID": msg.get("sessionID"),
            "time": time_info,
            "finish": msg.get("finish"),
            "model": msg.get("model") or {},
            "agent": msg.get("agent") or "",
        }
        err = _map_error(msg.get("error"))
        if err:
            info["error"] = err
        return {"info": info, "parts": _map_parts(msg)}

    return None


def _split_id(sid: str) -> dict:
    return {"id": sid}


async def _session_messages(sid: str) -> list[dict]:
    st, data = await v2("GET", f"/api/session/{sid}/message", timeout=30)
    if st != 200 or not isinstance(data, dict):
        return []
    out = []
    for m in data.get("data") or []:
        mapped = _map_message(m)
        if mapped:
            out.append(mapped)
    out.reverse()  # v2 llega del mas nuevo al mas viejo; v1 es al reves
    return out


def _map_session(s: dict) -> dict:
    return {
        "id": s.get("id"),
        "title": s.get("title") or s.get("id"),
        "directory": (s.get("location") or {}).get("directory") or s.get("directory") or "",
        "path": s.get("path") or "",
        "agent": s.get("agent") or "",
        "model": s.get("model") or {},
        "tokens": s.get("tokens") or {},
        "cost": s.get("cost") or 0,
        "time": s.get("time") or {},
    }


# ---------------------------------------------------------------- engine

async def _engine_ok() -> bool:
    st, _ = await v2("GET", "/api/session", timeout=6)
    return st == 200


async def _ensure_engine() -> bool:
    if not MANAGE_ENGINE:
        return await _engine_ok()
    if await _engine_ok():
        return True
    if not ENGINE_EXE or not os.path.exists(ENGINE_EXE):
        log.error(
            "motor v2 (opencode-cli.exe) no encontrado. Indica la ruta con OC_ENGINE "
            "o instala la app de escritorio de OpenCode."
        )
        return False
    log.info("arrancando motor v2 en %d", V2_PORT)
    env = dict(os.environ)
    env["OPENCODE_SERVER_PASSWORD"] = V2_PASS
    try:
        await asyncio.create_subprocess_exec(
            ENGINE_EXE, "serve", "--port", str(V2_PORT), "--hostname", "127.0.0.1",
            env=env, cwd=DEFAULT_DIR,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:
        log.error("no pude arrancar el motor: %s", e)
        return False
    for _ in range(40):
        await asyncio.sleep(0.5)
        if await _engine_ok():
            log.info("motor v2 listo")
            return True
    log.error("el motor v2 no respondio tras 20s")
    return False


async def _engine_watchdog():
    while True:
        await asyncio.sleep(20)
        try:
            if not await _engine_ok():
                log.warning("motor v2 caido, intento reiniciar")
                ok = await _ensure_engine()
                log.info("reinicio motor v2: %s", ok)
        except Exception as e:
            log.error("watchdog: %s", e)


# ---------------------------------------------------------------- handlers

async def h_sessions(request: web.Request):
    st, data = await v2("GET", "/api/session", timeout=30)
    if st != 200 or not isinstance(data, dict):
        return web.json_response({"error": "no pude listar sesiones", "v2": data}, status=502)
    return web.json_response([_map_session(s) for s in data.get("data") or []])


async def h_session_create(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = {"title": body.get("title") or "sesion telegram"}
    directory = body.get("directory") or DEFAULT_DIR
    if directory:
        payload["location"] = {"directory": directory}
    st, data = await v2("POST", "/api/session", payload, timeout=30)
    if st not in (200, 201) or not isinstance(data, dict):
        return web.json_response({"error": "no pude crear la sesion", "v2": data}, status=502)
    return web.json_response(_map_session(data.get("data") or {}))


async def h_session(request: web.Request):
    sid = request.match_info["sid"]
    st, data = await v2("GET", f"/api/session/{sid}", timeout=20)
    if st == 404:
        return web.json_response({"error": "sesion no encontrada"}, status=404)
    if st != 200 or not isinstance(data, dict):
        return web.json_response({"error": "error al leer sesion", "v2": data}, status=502)
    return web.json_response(_map_session(data.get("data") or {}))


async def h_messages(request: web.Request):
    sid = request.match_info["sid"]
    msgs = await _session_messages(sid)
    return web.json_response(msgs)


async def _set_model(sid: str, model: dict) -> bool:
    if not model:
        return True
    pid = model.get("providerID") or model.get("provider") or ""
    mid = model.get("id") or model.get("modelID") or ""
    if not (pid and mid):
        return True
    body = {"model": {"providerID": pid, "id": mid, "variant": model.get("variant") or "default"}}
    st, _ = await v2("POST", f"/api/session/{sid}/model", body, timeout=30)
    if st not in (200, 204):
        log.warning("no pude fijar modelo %s/%s en %s: %s", pid, mid, sid, st)
        return False
    return True


async def h_message_create(request: web.Request):
    sid = request.match_info["sid"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    texts = []
    for p in body.get("parts") or []:
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
            texts.append(p["text"])
    if not texts and body.get("text"):
        texts.append(body["text"])
    text = "\n".join(texts).strip()
    if not text:
        return web.json_response({"error": "mensaje vacio"}, status=400)

    model = body.get("model") or {}
    if not await _set_model(sid, model):
        # no es fatal: el motor usara el modelo de la sesion
        pass

    st, data = await v2("POST", f"/api/session/{sid}/prompt", {"text": text}, timeout=90)
    _recent_prompts[sid] = time.time()
    if st not in (200, 201, 202):
        detail = data
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("error") or json.dumps(detail)[:400]
        return web.json_response(
            {"name": "APIError", "data": {"message": str(detail)[:600]}}, status=400
        )
    return web.json_response({"info": {"id": (data or {}).get("data", {}).get("id", "")},
                              "parts": [{"type": "text", "text": text}]})


async def h_abort(request: web.Request):
    sid = request.match_info["sid"]
    st, data = await v2("POST", f"/api/session/{sid}/interrupt", {}, timeout=20)
    if st in (200, 202, 204):
        return web.json_response({"ok": True})
    return web.json_response({"error": "no pude interrumpir", "v2": data}, status=502)


async def h_provider(request: web.Request):
    st, data = await v2("GET", "/api/model", timeout=30)
    if st != 200 or not isinstance(data, dict):
        return web.json_response({"all": []})
    groups: dict[str, dict] = {}
    for m in data.get("data") or []:
        pid = m.get("providerID") or ""
        mid = m.get("modelID") or m.get("id") or ""
        if not (pid and mid):
            continue
        g = groups.setdefault(pid, {"id": pid, "name": pid, "models": {}})
        g["models"][mid] = {
            "id": mid,
            "name": m.get("name") or mid,
            "status": "active",
            "family": m.get("family") or "",
            "capabilities": m.get("capabilities") or {},
            "cost": m.get("cost") or [],
        }
    return web.json_response({"all": list(groups.values())})


async def _active_sessions() -> set[str]:
    now = time.time()
    if now - _active_cache["at"] < 2:
        return set(_active_cache["data"])
    sids: set[str] = set()
    st, data = await v2("GET", "/api/session/active", timeout=8)
    if st == 200 and isinstance(data, dict):
        d = data.get("data") or {}
        if isinstance(d, dict):
            for sid, info in d.items():
                if isinstance(info, dict) and info.get("type") == "running":
                    sids.add(sid)
    for sid, ts in list(_recent_prompts.items()):
        if now - ts < 300:
            sids.add(sid)
        else:
            _recent_prompts.pop(sid, None)
    _active_cache["at"] = now
    _active_cache["data"] = sids
    return sids


def _form_to_question(form: dict) -> dict:
    questions = []
    for f in form.get("fields") or []:
        opts = []
        for o in f.get("options") or []:
            if isinstance(o, dict):
                item = {"label": o.get("label") or o.get("value") or "?"}
                if o.get("description"):
                    item["description"] = o["description"]
                opts.append(item)
            else:
                opts.append({"label": str(o)})
        questions.append({
            "question": f.get("description") or f.get("title") or "?",
            "header": f.get("title") or "",
            "options": opts,
            "multiple": f.get("type") == "array",
        })
    return {
        "id": form.get("id"),
        "sessionID": form.get("sessionID"),
        "questions": questions,
    }


async def h_question_all(request: web.Request):
    out = []
    for sid in await _active_sessions():
        st, data = await v2("GET", f"/api/session/{sid}/form", timeout=10)
        if st != 200 or not isinstance(data, dict):
            continue
        forms = data.get("data") or []
        _forms_cache[sid] = forms
        for f in forms:
            out.append(_form_to_question(f))
    return web.json_response(out)


async def h_question_session(request: web.Request):
    sid = request.match_info["sid"]
    st, data = await v2("GET", f"/api/session/{sid}/form", timeout=10)
    if st != 200 or not isinstance(data, dict):
        return web.json_response([])
    forms = data.get("data") or []
    _forms_cache[sid] = forms
    return web.json_response([_form_to_question(f) for f in forms])


async def _reply_form(sid: str, form_id: str, answers) -> tuple[int, object]:
    forms = _forms_cache.get(sid) or []
    form = next((f for f in forms if f.get("id") == form_id), None)
    if form is None:
        st, data = await v2("GET", f"/api/session/{sid}/form", timeout=10)
        if st == 200 and isinstance(data, dict):
            forms = data.get("data") or []
            _forms_cache[sid] = forms
            form = next((f for f in forms if f.get("id") == form_id), None)
    fields = (form or {}).get("fields") or []
    keys = [f.get("key") for f in fields] or []
    answer: dict[str, str] = {}
    if isinstance(answers, list):
        for idx, val in enumerate(answers):
            key = keys[idx] if idx < len(keys) else f"q{idx}"
            if isinstance(val, (list, tuple)):
                val = val[0] if val else ""
            if val:
                answer[key] = str(val)
    elif isinstance(answers, dict):
        answer = {str(k): (v[0] if isinstance(v, (list, tuple)) and v else str(v)) for k, v in answers.items()}
    if not answer and keys:
        answer = {keys[0]: ""}
    return await v2("POST", f"/api/session/{sid}/form/{form_id}/reply", {"answer": answer}, timeout=20)


async def h_question_reply(request: web.Request):
    qid = request.match_info["qid"]
    sid = request.match_info.get("sid")
    try:
        body = await request.json()
    except Exception:
        body = {}
    answers = body.get("answers")
    if sid is None:
        for candidate, forms in list(_forms_cache.items()):
            if any(f.get("id") == qid for f in forms):
                sid = candidate
                break
    if sid is None:
        return web.json_response({"error": "no se de que sesion es la pregunta"}, status=404)
    st, data = await _reply_form(sid, qid, answers)
    if st in (200, 201, 204):
        forms = [f for f in _forms_cache.get(sid, []) if f.get("id") != qid]
        _forms_cache[sid] = forms
        return web.json_response({"ok": True})
    return web.json_response({"error": "no pude responder", "v2": data}, status=502)


async def h_permissions(request: web.Request):
    st, data = await v2("GET", "/api/permission/request", timeout=10)
    if st != 200 or not isinstance(data, dict):
        return web.json_response([])
    out = []
    for p in data.get("data") or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("id") or ""
        if not pid:
            continue
        sid = p.get("sessionID") or (p.get("session") or {}).get("id") or ""
        if sid:
            _permissions_cache[pid] = sid
        meta = p.get("metadata") or {}
        resource = p.get("resource") or p.get("patterns") or []
        if isinstance(resource, str):
            resource = [resource]
        out.append({
            "id": pid,
            "sessionID": sid,
            "permission": p.get("action") or p.get("permission") or p.get("type") or "?",
            "patterns": resource,
            "metadata": {"filepath": meta.get("filepath") or meta.get("path") or ""},
        })
    return web.json_response(out)


async def h_permission_reply(request: web.Request):
    pid = request.match_info["rid"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    reply = str(body.get("reply") or "always").lower()
    mapped = {"always": "always", "once": "once", "reject": "reject", "deny": "reject"}.get(reply, "always")
    sid = _permissions_cache.get(pid)
    if not sid:
        st, data = await v2("GET", "/api/permission/request", timeout=10)
        for p in (data or {}).get("data", []) if isinstance(data, dict) else []:
            if isinstance(p, dict) and p.get("id") == pid:
                sid = p.get("sessionID") or ""
                break
    if not sid:
        return web.json_response({"error": "permiso desconocido"}, status=404)
    st, data = await v2("POST", f"/api/session/{sid}/permission/{pid}/reply", {"reply": mapped}, timeout=20)
    if st in (200, 201, 204):
        _permissions_cache.pop(pid, None)
        return web.json_response({"ok": True})
    return web.json_response({"error": "no pude responder permiso", "v2": data}, status=502)


async def h_health(request: web.Request):
    ok = await _engine_ok()
    return web.json_response({"shim": "ok", "engine": "ok" if ok else "down", "engine_url": V2_URL})


async def h_missing(request: web.Request):
    log.info("ruta no emulada: %s %s", request.method, request.path)
    return web.json_response({"error": "ruta no soportada por el shim", "path": request.path}, status=404)


# ---------------------------------------------------------------- app

async def _on_startup(app):
    ok = await _ensure_engine()
    log.info("motor v2 disponible: %s (%s)", ok, V2_URL)
    app["watchdog"] = asyncio.create_task(_engine_watchdog())


async def _on_cleanup(app):
    task = app.get("watchdog")
    if task:
        task.cancel()


def build_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/health", h_health)

    app.router.add_get("/session", h_sessions)
    app.router.add_post("/session", h_session_create)
    app.router.add_get("/session/{sid}", h_session)
    app.router.add_get("/session/{sid}/message", h_messages)
    app.router.add_post("/session/{sid}/message", h_message_create)
    app.router.add_post("/session/{sid}/abort", h_abort)

    app.router.add_get("/provider", h_provider)

    app.router.add_get("/question", h_question_all)
    app.router.add_get("/api/session/{sid}/question", h_question_session)
    app.router.add_post("/question/{qid}/reply", h_question_reply)
    app.router.add_post("/api/session/{sid}/question/{qid}/reply", h_question_reply)

    app.router.add_get("/permission", h_permissions)
    app.router.add_post("/permission/{rid}/reply", h_permission_reply)

    app.router.add_route("*", "/{tail:.*}", h_missing)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main():
    setup_logging()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    log.info("shim v1->v2 escuchando en 127.0.0.1:%d -> %s", SHIM_PORT, V2_URL)
    web.run_app(build_app(), host="127.0.0.1", port=SHIM_PORT, print=None)


if __name__ == "__main__":
    main()
