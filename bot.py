import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

PID_FILE = os.path.join(os.path.dirname(__file__), "bot.pid")

def _write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def _remove_pid():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass

_write_pid()

LOG_FILE = os.path.join(os.path.dirname(__file__), "bot.log")
_log_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_log_handler],
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENCODE_URL = os.environ["OPENCODE_URL"]
OPENCODE_USER = os.environ["OPENCODE_USER"]
OPENCODE_PASS = os.environ["OPENCODE_PASS"]

SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")
ARCHIVED_FILE = os.path.join(os.path.dirname(__file__), "archived_sessions.json")
MODELS_FILE = os.path.join(os.path.dirname(__file__), "models.json")
ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), "attachments")
OPENCODE_DB = os.environ.get(
    "OPENCODE_DB",
    os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db"),
)
DEFAULT_MODEL = {"providerID": "opencode", "modelID": "big-pickle"}
user_sessions: dict[int, str] = {}
chat_model: dict[int, dict] = {}
pending_selection: set[int] = set()
pending_archive: set[int] = set()
pending_unarchive: set[int] = set()
pending_model: set[int] = set()
known_permissions: dict[str, int] = {}
known_questions: dict[str, dict] = {}
pending_question_reply: dict[int, tuple[str, int]] = {}
skip_broken_check: set[int] = set()
active_tasks: dict[int, asyncio.Task] = {}
active_thinking: dict[int, int] = {}
thinking_started: dict[int, float] = {}
last_activity: dict[int, float] = {}
_activity_sig: dict[int, str] = {}
live_stream: dict[int, dict] = {}
live_mids: set = set()
sent_tool_count: dict[str, int] = {}
sent_reasoning_ids: set[str] = set()
_question_counter: int = 0
_short_to_call: dict[str, str] = {}
_pending_rich_md: dict[int, str] = {}
shown_final_ids: set[str] = set()
primed_sessions: set[str] = set()
_mirror_lock = asyncio.Lock()
ALLOW_LIVE = False
_tool_msg: dict[int, dict] = {}
deliver_tries: dict[str, int] = {}
_shown_inter_texts: set[str] = set()
_intentional_aborts: dict[str, float] = {}


def load_sessions() -> dict[int, str]:
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, "r") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    return {}


def save_sessions(sessions: dict[int, str]):
    with open(SESSIONS_FILE, "w") as f:
        json.dump({str(k): v for k, v in sessions.items()}, f)


def load_archived() -> set[str]:
    if os.path.exists(ARCHIVED_FILE):
        with open(ARCHIVED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_archived(archived: set[str]):
    with open(ARCHIVED_FILE, "w") as f:
        json.dump(sorted(archived), f)


user_sessions = load_sessions()
archived_sessions = load_archived()

TURN_OWNER_FILE = os.path.join(os.path.dirname(__file__), "turn_owner.json")
DELIVERED_FILE = os.path.join(os.path.dirname(__file__), "delivered_finals.json")


def load_delivered() -> set[str]:
    if os.path.exists(DELIVERED_FILE):
        try:
            with open(DELIVERED_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _mark_delivered(mid: str):
    if mid in delivered_finals:
        return
    delivered_finals.add(mid)
    try:
        with open(DELIVERED_FILE, "w") as f:
            json.dump(sorted(delivered_finals), f)
    except Exception:
        pass


delivered_finals = load_delivered()


def load_turn_owner() -> dict[str, int]:
    if os.path.exists(TURN_OWNER_FILE):
        with open(TURN_OWNER_FILE, "r") as f:
            return {k: int(v) for k, v in json.load(f).items()}
    return {}


def save_turn_owner(owner: dict[str, int]):
    with open(TURN_OWNER_FILE, "w") as f:
        json.dump(owner, f)


turn_owner: dict[str, int] = load_turn_owner()


def load_chat_models() -> dict[int, dict]:
    if os.path.exists(MODELS_FILE):
        with open(MODELS_FILE, "r") as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_chat_models(models: dict[int, dict]):
    with open(MODELS_FILE, "w") as f:
        json.dump({str(k): v for k, v in models.items()}, f, indent=2)


chat_model = load_chat_models()


def get_auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(OPENCODE_USER, OPENCODE_PASS)


def _read_v2_sessions() -> dict[str, dict]:
    """Titulos y fechas de la tabla session_v2 (la que usa la app de escritorio)."""
    out: dict[str, dict] = {}
    if not os.path.exists(OPENCODE_DB):
        return out
    uri = "file:" + OPENCODE_DB.replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            for sid, title, upd in con.execute(
                "select id, title, time_updated from session_v2"
            ):
                out[sid] = {"title": title or "", "updated": int(upd or 0)}
        finally:
            con.close()
    except Exception as e:
        logging.warning("no pude leer session_v2: %s", str(e))
    return out


async def fetch_sessions() -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{OPENCODE_URL}/session", auth=get_auth())
        r.raise_for_status()
        sessions = r.json()

    v2 = await asyncio.to_thread(_read_v2_sessions)
    known = {s.get("id") for s in sessions}
    for s in sessions:
        meta = v2.get(s.get("id"))
        if not meta:
            continue
        if meta["title"]:
            s["title"] = meta["title"]
        t = s.get("time") or {}
        t["updated"] = max(int(t.get("updated") or 0), meta["updated"])
        s["time"] = t
    for sid, meta in v2.items():
        if sid in known:
            continue
        sessions.append({
            "id": sid,
            "title": meta["title"] or sid,
            "time": {"updated": meta["updated"]},
            "tokens": {},
            "_desktop_only": True,
        })
    sessions.sort(key=lambda s: int((s.get("time") or {}).get("updated") or 0), reverse=True)
    return sessions


async def get_session_model(session_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{OPENCODE_URL}/session/{session_id}", auth=get_auth())
            data = r.json()
        model = data.get("model") or {}
        pid = model.get("providerID") or "opencode"
        mid = model.get("id") or model.get("modelID")
        if mid:
            return {"providerID": pid, "modelID": mid}
    except Exception as e:
        logging.warning("get_session_model error: %s", str(e))
    return {}


async def fetch_provider_models(provider_ids: list[str] | None = None) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{OPENCODE_URL}/provider", auth=get_auth())
            data = r.json()
        out: list[dict] = []
        for prov in data.get("all", []):
            pid = prov.get("id", "")
            if provider_ids and pid not in provider_ids:
                continue
            for key, info in (prov.get("models") or {}).items():
                if info.get("status") == "disabled":
                    continue
                out.append({
                    "key": key,
                    "providerID": pid,
                    "modelID": info.get("id") or key,
                    "name": info.get("name") or key,
                })
        out.sort(key=lambda m: m["name"].lower())
        return out
    except Exception as e:
        logging.warning("fetch_provider_models error: %s", str(e))
        return []


def format_session_list(sessions: list[dict], include_archived: bool = False) -> str:
    visible = [s for s in sessions if include_archived or s.get("id") not in archived_sessions]
    if not visible:
        return "No hay sesiones disponibles." if not include_archived else "No hay sesiones archivadas."
    label = "Sesiones archivadas" if include_archived else "Sesiones disponibles"
    lines = [f"{label}:\n"]
    for i, s in enumerate(visible, 1):
        title = s.get("title", s.get("id", "?"))
        if s.get("_desktop_only"):
            title += "  [solo escritorio]"
        tokens = s.get("tokens") or {}
        total = (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0)
        if not total:
            lines.append(f"{i}. {title}")
            continue
        if total > 100000:
            tag = " (pesada)"
        elif total > 30000:
            tag = " (media)"
        else:
            tag = ""
        lines.append(f"{i}. {title}  ({total:,} tokens){tag}")
    if include_archived:
        lines.append("\nResponde con el numero para desarchivar, o 'cancelar'.")
    else:
        lines.append("\nResponde con el numero de sesion, o escribe 'nueva' para crear una.")
    return "\n".join(lines)


async def show_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        sessions = await fetch_sessions()
        text = format_session_list(sessions)
        pending_selection.add(update.effective_chat.id)
        cache_file = os.path.join(os.path.dirname(__file__), "all_sessions_cache.json")
        with open(cache_file, "w") as f:
            json.dump(sessions, f)
    except Exception as e:
        text = f"Error al obtener sesiones: {e}"
    if len(text) > 4000:
        await _send_chunked_plain(context.bot, update.effective_chat.id, text)
    else:
        await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in user_sessions:
        await update.message.reply_text(
            "Ya tienes una sesion activa.\nEscribe /sesiones para cambiar, o continua hablando."
        )
    else:
        await show_sessions(update, context)


async def cmd_sesiones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_sessions(update, context)


async def cmd_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in user_sessions:
        del user_sessions[chat_id]
        save_sessions(user_sessions)
    await update.message.reply_text("Sesion limpia. Envia un mensaje para crear una nueva.")


async def cmd_archivar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        sessions = await fetch_sessions()
        visible = [s for s in sessions if s.get("id") not in archived_sessions]
        if not visible:
            await update.message.reply_text("No hay sesiones para archivar.")
            return
        lines = ["Sesiones para archivar:\n"]
        for i, s in enumerate(visible, 1):
            lines.append(f"{i}. {s.get('title', s.get('id', '?'))}")
        lines.append("\nResponde con el numero para archivar, o 'cancelar'.")
        pending_archive.add(update.effective_chat.id)
        cache_file = os.path.join(os.path.dirname(__file__), "all_sessions_cache.json")
        with open(cache_file, "w") as f:
            json.dump(visible, f)
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error al obtener sesiones: {e}")


async def cmd_desarchivar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        sessions = await fetch_sessions()
        archived_list = [s for s in sessions if s.get("id") in archived_sessions]
        if not archived_list:
            await update.message.reply_text("No hay sesiones archivadas.")
            return
        lines = ["Sesiones archivadas:\n"]
        for i, s in enumerate(archived_list, 1):
            lines.append(f"{i}. {s.get('title', s.get('id', '?'))}")
        lines.append("\nResponde con el numero para desarchivar, o 'cancelar'.")
        pending_unarchive.add(update.effective_chat.id)
        cache_file = os.path.join(os.path.dirname(__file__), "all_sessions_cache.json")
        with open(cache_file, "w") as f:
            json.dump(archived_list, f)
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error al obtener sesiones: {e}")


_model_choice_cache: dict[int, list[dict]] = {}


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.chat.send_action("typing")
    session_id = user_sessions.get(chat_id)
    current = None
    if chat_id in chat_model:
        current = dict(chat_model[chat_id])
    elif session_id:
        current = await get_session_model(session_id)
    if current:
        current_disp = f"{current.get('providerID')}/{current.get('modelID')}"
    else:
        current_disp = "por defecto (el de la sesión)"

    models = await fetch_provider_models(["opencode", "opencode-go"])
    if not models:
        await update.message.reply_text(
            f"Modelo actual: {current_disp}\nNo pude obtener la lista de modelos disponibles."
        )
        return

    _model_choice_cache[chat_id] = models

    lines = [f"Modelo actual: {current_disp}\n", "Modelos disponibles:"]
    for idx, m in enumerate(models, 1):
        tag = ""
        if current and m["providerID"] == current.get("providerID") and m["modelID"] == current.get("modelID"):
            tag = "  ✅"
        lines.append(f"{idx}. {m['name']} ({m['providerID']}){tag}")
    lines.append("\nResponde con el numero, o pulsa Cancelar.")
    pending_model.add(chat_id)
    keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="sm_cancel")]]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if query.data == "sm_cancel":
        pending_model.discard(chat_id)
        _model_choice_cache.pop(chat_id, None)
        await query.edit_message_text("Cancelado.")
        return


async def _create_new_session(client: httpx.AsyncClient, chat_id: int) -> str:
    now = datetime.now().strftime("%Y%m%d-%H%M")
    r = await client.post(
        f"{OPENCODE_URL}/session",
        json={"title": f"telegram-{chat_id}-{now}"},
        auth=get_auth(),
    )
    r.raise_for_status()
    session_id = r.json()["id"]
    user_sessions[chat_id] = session_id
    save_sessions(user_sessions)
    logging.info("chat %d -> nueva sesion opencode %s", chat_id, session_id)
    return session_id


class SessionBrokenError(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_CODEBLOCK_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_TABLE_SEP_RE = re.compile(r"^[:\-\s|]+$")


def _table_block(lines):
    rows = []
    for line in lines:
        body = line.strip()
        if body.startswith("|"):
            body = body[1:]
        if body.endswith("|"):
            body = body[:-1]
        rows.append([c.strip() for c in body.split("|")])
    if not rows:
        return None
    has_header = len(rows) > 1 and all(
        _MD_TABLE_SEP_RE.match(c) for c in rows[1]
    )

    def is_sep(r):
        return all(_MD_TABLE_SEP_RE.match(c) for c in r)

    if has_header:
        header = rows[0]
        data = [r for i, r in enumerate(rows) if i != 0 and not is_sep(r)]
    else:
        header = None
        data = [r for r in rows if not is_sep(r)]
    if not data and not header:
        return None
    out = []
    if header:
        out.append(" · ".join("<b>%s</b>" % _escape_html(c) for c in header))
    for r in data:
        out.append(" — ".join(_escape_html(c) for c in r))
    return "\n".join(out)


def _md_to_telegram_html(text: str) -> str:
    if not text:
        return ""

    def _inline_single(seg: str) -> str:
        seg = _escape_html(seg)
        seg = _MD_HEADER_RE.sub(lambda m: "<b>%s</b>" % m.group(2), seg)
        seg = _MD_LINK_RE.sub(
            lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), seg
        )
        seg = _MD_BOLD_RE.sub(lambda m: "<b>%s</b>" % m.group(1), seg)
        seg = _MD_ITALIC_RE.sub(lambda m: "<i>%s</i>" % m.group(1), seg)
        seg = _MD_STRIKE_RE.sub(lambda m: "<s>%s</s>" % m.group(1), seg)
        seg = _MD_INLINE_CODE_RE.sub(lambda m: "<code>%s</code>" % m.group(1), seg)
        return seg

    def _inline(seg: str) -> str:
        lines = seg.split("\n")
        out = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if line.strip().startswith("|") and "|" in line[1:]:
                block = []
                j = i
                while j < n and lines[j].strip().startswith("|"):
                    block.append(lines[j])
                    j += 1
                tb = _table_block(block)
                if tb is not None:
                    out.append(tb)
                    i = j
                    continue
            out.append(_inline_single(line))
            i += 1
        return "\n".join(out)

    parts = []
    rest = text
    for m in _MD_CODEBLOCK_RE.finditer(text):
        parts.append(_inline(rest[: m.start()]))
        lang = m.group(1).strip()
        code = _escape_html(m.group(2).strip("\n"))
        parts.append("<pre>%s</pre>" % ("<code>%s</code>" % code if lang else code))
        rest = text[m.end():]
    parts.append(_inline(rest))
    return "".join(parts)


async def _post_checked(client, *args, **kwargs):
    r = await client.post(*args, **kwargs)
    r.raise_for_status()
    return r


def _extract_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "Timeout al comunicarme con opencode (no respondio a tiempo). Reintenta, o si el run atasca la sesion, espera a que expire o envia otro mensaje."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else "?"
        body = ""
        try:
            data = exc.response.json()
            body = f"{data.get('name', '')}: {json.dumps(data.get('data', {}), ensure_ascii=False)}"
        except Exception:
            body = (exc.response.text or "")[:500]
        return f"Error {status} de opencode:\n{body[:700]}"
    text = str(exc)
    if not text or not text.strip():
        return "Error inesperado de opencode (sin detalle)."
    if len(text) > 700:
        text = text[:700] + "..."
    return f"Error de opencode: {text}"


async def _send_error_to_tg(bot, chat_id: int, err_text: str, thinking_msg_id: int):
    msg = "⚠️ " + err_text
    if thinking_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=thinking_msg_id,
                text=msg,
            )
            return
        except Exception:
            pass
    try:
        await bot.send_message(chat_id=chat_id, text=msg)
    except Exception:
        pass


async def _send_rich_markdown(bot, chat_id: int, md: str):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendRichMessage"
    payload = {"chat_id": chat_id, "rich_message": {"markdown": md}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()


def _retry_after_seconds(e: Exception):
    ra = getattr(e, "retry_after", None)
    if ra:
        return float(ra)
    m = re.search(r"retry after[:\s]*(\d+)", str(e), re.IGNORECASE)
    if m:
        return float(m.group(1))
    return 0.0


def _html_to_plain(html: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", html)
    txt = re.sub(r"<[^>]+>", "", txt)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        txt = txt.replace(ent, ch)
    return txt


async def _send_chunked_plain(bot, chat_id: int, text: str):
    MAX_MSG = 4000
    text = text.strip()
    while text:
        chunk = text[:MAX_MSG]
        if len(text) > MAX_MSG:
            cut = chunk.rfind("\n")
            if cut > MAX_MSG // 2:
                chunk = chunk[:cut]
        chunk = chunk.strip()
        if chunk:
            await bot.send_message(chat_id=chat_id, text=chunk)
        text = text[len(chunk):].lstrip("\n")


async def _send_final_response(bot, chat_id: int, html: str):
    global _pending_rich_md
    rich_md = _pending_rich_md.pop(chat_id, None)
    if rich_md:
        try:
            await _send_rich_markdown(bot, chat_id, rich_md)
            return
        except Exception as e:
            logging.warning("[%d] sendRichMessage fallo: %s", chat_id, str(e))
    if len(html) > 4000:
        try:
            await _send_chunked_plain(bot, chat_id, _html_to_plain(html))
            return
        except Exception as e:
            logging.warning("[%d] envio fragmentado fallo: %s", chat_id, str(e))
    last = None
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=html, parse_mode="HTML")
            return
        except Exception as e:
            last = e
            wait = _retry_after_seconds(e)
            if wait:
                logging.warning("[%d] 429 en entrega final, reintento en %.0fs", chat_id, wait)
                await asyncio.sleep(min(wait, 10))
            else:
                await asyncio.sleep(1 + attempt * 2)
    try:
        await bot.send_message(chat_id=chat_id, text=html)
    except Exception as e:
        raise e


async def _deliver_final(bot, chat_id: int, mid: str, formatted: str, thinking_msg_id: int = None) -> bool:
    async with _mirror_lock:
        if mid in shown_final_ids:
            return False
        shown_final_ids.add(mid)
    try:
        if _pending_rich_md.get(chat_id) and thinking_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=thinking_msg_id)
            except Exception:
                pass
            await _send_final_response(bot, chat_id, formatted)
            _mark_delivered(mid)
            return True
        if thinking_msg_id:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=thinking_msg_id, text=formatted, parse_mode="HTML")
                _mark_delivered(mid)
                return True
            except Exception:
                pass
        await _send_final_response(bot, chat_id, formatted)
        _mark_delivered(mid)
        return True
    except Exception as e:
        logging.error("[%d] fallo al entregar final %s, se reintentara: %s", chat_id, mid, str(e))
        async with _mirror_lock:
            shown_final_ids.discard(mid)
        deliver_tries[mid] = deliver_tries.get(mid, 0) + 1
        if deliver_tries[mid] >= 6:
            async with _mirror_lock:
                shown_final_ids.add(mid)
            logging.error("[%d] final %s descartado tras %d intentos", chat_id, mid, deliver_tries[mid])
        return False


def _has_empty_tool_parts(msg: dict) -> bool:
    for p in msg.get("parts", []):
        if p.get("type") != "tool":
            continue
        state = p.get("state", {})
        inp = state.get("input", {})
        output = state.get("output", "")
        has_input = bool(inp) and (not isinstance(inp, dict) or any(inp.values()))
        has_output = bool(output) and str(output).strip()
        if has_input and not has_output:
            return True
    return False


def _tool_parts_with_content(msg: dict) -> bool:
    for p in msg.get("parts", []):
        if p.get("type") != "tool":
            continue
        state = p.get("state", {})
        inp = state.get("input", {})
        has_input = bool(inp) and (not isinstance(inp, dict) or any(inp.values()))
        if has_input:
            return True
    return False


def _format_tool_html(tool_name: str, state: dict) -> str:
    inp = state.get("input", {})
    output = state.get("output", "")
    out = output if isinstance(output, str) else str(output)

    if tool_name == "bash":
        cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
        return "<b>&#9889; Shell</b>\n<code>" + _escape_html(cmd[:500]) + "</code>\n<b>Output:</b>\n<code>" + _escape_html(out[:1000]) + "</code>"
    elif tool_name == "write":
        fp = inp.get("filePath", "?") if isinstance(inp, dict) else "?"
        return "<b>&#128221; Write:</b> <code>" + _escape_html(fp) + "</code>\n<code>" + _escape_html(out[:500]) + "</code>"
    elif tool_name == "read":
        fp = inp.get("filePath", "?") if isinstance(inp, dict) else "?"
        preview = out[:800] + ("..." if len(out) > 800 else "")
        return "<b>&#128214; Read:</b> <code>" + _escape_html(fp) + "</code>\n<code>" + _escape_html(preview) + "</code>"
    elif tool_name == "edit":
        fp = inp.get("filePath", "?") if isinstance(inp, dict) else "?"
        return "<b>&#9998; Edit:</b> <code>" + _escape_html(fp) + "</code>\n<code>" + _escape_html(out[:500]) + "</code>"
    elif tool_name == "grep":
        pattern = inp.get("pattern", "?") if isinstance(inp, dict) else "?"
        return "<b>&#128269; Grep:</b> <code>" + _escape_html(pattern) + "</code>\n<code>" + _escape_html(out[:800]) + "</code>"
    elif tool_name == "question":
        qs = inp.get("questions", inp) if isinstance(inp, dict) else [inp]
        qtexts = []
        for q in qs if isinstance(qs, list) else [qs]:
            qtext = q.get("question", str(q)) if isinstance(q, dict) else str(q)
            qtexts.append(str(qtext)[:200])
        qstr = " | ".join(qtexts)
        status = state.get("status", "")
        error = state.get("error", "")
        out_str = out if isinstance(out, str) else str(out) if out else ""
        msg = "<b>&#10067; Pregunta:</b> " + _escape_html(qstr)
        if status == "pending":
            msg += "\n<i>(esperando respuesta...)</i>"
        elif status == "error":
            msg += "\n<b>Error:</b> " + _escape_html(error[:300])
        if out_str:
            msg += "\n<b>Respuesta:</b> " + _escape_html(out_str[:300])
        return msg
    elif tool_name == "websearch":
        q = inp.get("query", "?") if isinstance(inp, dict) else "?"
        return "<b>&#128270; WebSearch:</b> " + _escape_html(str(q)[:200])
    elif tool_name == "webfetch":
        url_val = inp.get("url", "?") if isinstance(inp, dict) else "?"
        return "<b>&#127760; WebFetch:</b> " + _escape_html(str(url_val)[:200]) + "\n<code>" + _escape_html(out[:500]) + "</code>"
    elif tool_name == "todowrite":
        return "<b>&#9745; TodoList actualizada</b>"
    else:
        return "<b>&#128295; " + tool_name + ":</b>\n<code>" + _escape_html(out[:800]) + "</code>"


async def _restore_live_last(bot, chat_id: int):
    live = live_stream.get(chat_id)
    if not live:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=live["msg_id"])
    except Exception:
        return
    try:
        nb = await bot.send_message(chat_id=chat_id, text=live.get("text") or "⏳ Trabajando...")
    except Exception:
        return
    live["msg_id"] = nb.message_id


def _plain_text_of(msg: dict) -> str:
    parts = []
    for p in msg.get("parts", []):
        if p.get("type") == "text":
            t = p.get("text", "")
            if t:
                parts.append(t)
    return "\n\n".join(parts).strip()


def _format_tool_short(tool_name: str, inp) -> str:
    dat = inp if isinstance(inp, dict) else {}
    if tool_name == "bash":
        cmd = str(dat.get("command", ""))[:120]
        return "Ejecutando: " + cmd
    elif tool_name == "write":
        return "Escribiendo: " + str(dat.get("filePath", "?"))
    elif tool_name == "edit":
        return "Editando: " + str(dat.get("filePath", "?"))
    elif tool_name == "read":
        return "Leyendo: " + str(dat.get("filePath", "?"))
    elif tool_name == "grep":
        return "Buscando: " + str(dat.get("pattern", "?"))
    elif tool_name == "websearch":
        return "Buscando en la web: " + str(dat.get("query", "?"))
    elif tool_name == "webfetch":
        return "Leyendo web: " + str(dat.get("url", "?"))
    elif tool_name == "todowrite":
        return "Actualizando lista de tareas"
    elif tool_name == "question":
        return "Te esta haciendo una pregunta"
    return tool_name


def _extract_reasoning(msg: dict) -> str:
    parts = []
    for p in msg.get("parts", []):
        if p.get("type") == "reasoning":
            text = p.get("text", "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _extract_table_blocks(text):
    blocks = []
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.strip().startswith("|") and "|" in line[1:]:
            j = i
            block = []
            while j < n and lines[j].strip().startswith("|"):
                block.append(lines[j])
                j += 1
            blocks.append("\n".join(block))
            i = j
        else:
            i += 1
    return blocks


def _format_full_response(msg: dict, skip_reasoning: bool = False, chat_id: int | None = None) -> str:
    global _pending_rich_md
    sections = []
    md_sections = []
    has_table = False
    for p in msg.get("parts", []):
        ptype = p.get("type", "")
        if ptype == "text":
            raw = p.get("text", "")
            sections.append(_md_to_telegram_html(raw))
            md_sections.append(raw)
            if _extract_table_blocks(raw):
                has_table = True
    if has_table and chat_id is not None:
        _pending_rich_md[chat_id] = "\n\n".join(md_sections)
    elif chat_id is not None:
        _pending_rich_md.pop(chat_id, None)
    result = "\n\n".join(sections)
    return result if result and result.strip() else "(sin respuesta de texto)"


def _format_new_tools(msg: dict, skip_count: int) -> str:
    sections = []
    tool_idx = 0
    for p in msg.get("parts", []):
        ptype = p.get("type", "")
        if ptype == "tool":
            state = p.get("state", {})
            inp = state.get("input", {})
            has_input = bool(inp) and (not isinstance(inp, dict) or any(inp.values()))
            if has_input:
                if tool_idx >= skip_count:
                    sections.append(_format_tool_html(p.get("tool", "?"), state))
                tool_idx += 1
    result = "\n\n".join(sections)
    return result if result and result.strip() else ""


def _tool_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Ver detalle", callback_data="tool_toggle")]])


def _tool_kb_expand():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Ocultar detalle", callback_data="tool_toggle")]])


async def _send_collapsed_tool(bot, chat_id: int, tool_name: str, state: dict):
    inp = state.get("input", {})
    short = _format_tool_short(tool_name, inp)
    full = _format_tool_html(tool_name, state)
    collapsed = "<b>&#128736; " + _escape_html(short) + "</b>\n<code>(toca para ver el detalle)</code>"
    try:
        msg = await bot.send_message(chat_id=chat_id, text=collapsed, parse_mode="HTML", reply_markup=_tool_kb())
        _tool_msg[msg.message_id] = {"collapsed": collapsed, "full": full, "state": "collapsed"}
    except Exception:
        pass


async def _send_tools_collapsed(bot, chat_id: int, msg: dict, skip_count: int):
    idx = 0
    for p in msg.get("parts", []):
        if p.get("type") != "tool":
            continue
        state = p.get("state", {})
        inp = state.get("input", {})
        if not (bool(inp) and (not isinstance(inp, dict) or any(inp.values()))):
            continue
        if p.get("tool") == "question":
            continue
        if idx >= skip_count:
            await _send_collapsed_tool(bot, chat_id, p.get("tool", "?"), state)
        idx += 1
    return idx


async def _handle_tool_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = _tool_msg.get(query.message.message_id)
    if not data:
        return
    if data.get("state") == "collapsed":
        await query.edit_message_text(text=data["full"], parse_mode="HTML", reply_markup=_tool_kb_expand())
        data["state"] = "expanded"
    else:
        await query.edit_message_text(text=data["collapsed"], parse_mode="HTML", reply_markup=_tool_kb())
        data["state"] = "collapsed"


def _count_completed_tools(msg: dict) -> int:
    count = 0
    for p in msg.get("parts", []):
        if p.get("type") != "tool":
            continue
        if p.get("tool") == "question":
            continue
        state = p.get("state", {})
        inp = state.get("input", {})
        has_input = bool(inp) and (not isinstance(inp, dict) or any(inp.values()))
        if has_input:
            count += 1
    return count


async def _send_permission_message(bot, chat_id: int, perm: dict):
    req_id = perm.get("id")
    if not req_id or req_id in known_permissions:
        return
    perm_type = perm.get("permission", "?")
    patterns = perm.get("patterns", [])
    metadata = perm.get("metadata", {})
    filepath = metadata.get("filepath", "")
    resource_str = "\n".join("  - " + p for p in patterns[:5])
    if len(patterns) > 5:
        resource_str += "\n  ... y " + str(len(patterns) - 5) + " mas"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Una vez", callback_data="perm_once:" + req_id),
            InlineKeyboardButton("Siempre", callback_data="perm_always:" + req_id),
            InlineKeyboardButton("Denegar", callback_data="perm_reject:" + req_id),
        ]
    ])
    msg = "<b>Permiso requerido:</b>\nTipo: " + _escape_html(perm_type) + "\n"
    if filepath:
        msg += "Ruta: <code>" + _escape_html(filepath) + "</code>\n"
    if patterns:
        msg += "Patrones:\n<code>" + _escape_html(resource_str) + "</code>"
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", reply_markup=keyboard)
    known_permissions[req_id] = chat_id
    await _restore_live_last(bot, chat_id)


async def _resolve_question_request_id(session_id: str, questions: list) -> str:
    if not session_id or not questions:
        return ""
    first_q = ""
    if isinstance(questions[0], dict):
        first_q = questions[0].get("question", "")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            urls = [
                OPENCODE_URL + "/api/session/" + session_id + "/question",
                OPENCODE_URL + "/question",
            ]
            for url in urls:
                r = await client.get(url, auth=get_auth())
                if r.status_code != 200:
                    continue
                try:
                    payload = r.json()
                except Exception:
                    continue
                items = payload.get("data", []) if isinstance(payload, dict) else payload
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    qid = item.get("id", "")
                    if not qid:
                        continue
                    if item.get("sessionID") and item.get("sessionID") != session_id:
                        continue
                    iq = item.get("questions", [])
                    if len(iq) != len(questions):
                        continue
                    if not iq:
                        continue
                    if isinstance(iq[0], dict) and iq[0].get("question", "") != first_q:
                        continue
                    return qid
    except Exception as e:
        logging.error("_resolve_question_request_id: %s", str(e))
    return ""


def _question_already_sent(session_id: str, questions: list) -> bool:
    if not session_id or not questions:
        return False
    first_q = ""
    if isinstance(questions[0], dict):
        first_q = questions[0].get("question", "")
    for info in known_questions.values():
        if info.get("session_id") != session_id:
            continue
        iq = info.get("questions", [])
        if not iq:
            continue
        if isinstance(iq[0], dict) and iq[0].get("question", "") != first_q:
            continue
        return True
    return False


async def _send_question_buttons_from_tool(bot, chat_id: int, session_id: str, part: dict):
    global _question_counter
    state = part.get("state", {})
    inp = state.get("input", {})
    questions = inp.get("questions", [])
    if not questions:
        return
    call_id = part.get("callID", "")
    if not call_id:
        return
    if call_id in known_questions or _question_already_sent(session_id, questions):
        return
    _question_counter += 1
    short_key = "c%d" % _question_counter
    _short_to_call[short_key] = call_id
    known_questions[call_id] = {
        "chat_id": chat_id,
        "session_id": session_id,
        "num": len(questions),
        "short": short_key,
        "questions": questions,
    }
    for qidx, q in enumerate(questions):
        q_question = q.get("question", "?")
        q_header = q.get("header", "")
        q_options = q.get("options", [])
        msg = "<b>&#10067; Pregunta:</b>\n"
        if q_header:
            msg += "<b>" + _escape_html(q_header) + "</b>\n"
        msg += _escape_html(q_question)
        buttons = []
        for idx, opt in enumerate(q_options):
            if isinstance(opt, dict):
                label = opt.get("label", "?")
            else:
                label = str(opt)
            buttons.append(InlineKeyboardButton(
                label,
                callback_data="q_a:%s:%d:%d" % (short_key, qidx, idx),
            ))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([
            InlineKeyboardButton("✏️ Escribir", callback_data="q_w:%s:%d" % (short_key, qidx)),
        ])
        keyboard = InlineKeyboardMarkup(rows) if rows else None
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", reply_markup=keyboard)
            logging.info("_send_question_buttons_from_tool: sent question %s qidx=%d with %d options", short_key, qidx, len(q_options))
        except Exception as e:
            logging.error("_send_question_buttons_from_tool: error qidx=%d: %s", qidx, str(e))
    await _restore_live_last(bot, chat_id)


async def _send_question_message(bot, chat_id: int, req: dict):
    global _question_counter
    req_id = req.get("id")
    questions = req.get("questions", [])
    if not req_id or not questions:
        logging.error("_send_question_message: empty req_id or questions")
        return
    if req_id in known_questions or _question_already_sent(req.get("sessionID", ""), questions):
        logging.info("_send_question_message: already sent %s", req_id)
        return
    _question_counter += 1
    short_key = "c%d" % _question_counter
    _short_to_call[short_key] = req_id
    known_questions[req_id] = {
        "chat_id": chat_id,
        "session_id": req.get("sessionID", ""),
        "num": len(questions),
        "short": short_key,
        "questions": questions,
    }
    for qidx, q in enumerate(questions):
        q_question = q.get("question", "?")
        q_header = q.get("header", "")
        q_options = q.get("options", [])
        q_multiple = q.get("multiple", False)

        msg = "<b>&#10067; Pregunta:</b>\n"
        if q_header:
            msg += "<b>" + _escape_html(q_header) + "</b>\n"
        msg += _escape_html(q_question)
        if q_multiple:
            msg += "\n<i>(puedes elegir varias)</i>"

        buttons = []
        for idx, opt in enumerate(q_options):
            if isinstance(opt, dict):
                label = opt.get("label", "?")
            else:
                label = str(opt)
            buttons.append(InlineKeyboardButton(
                label,
                callback_data="q_a:%s:%d:%d" % (short_key, qidx, idx),
            ))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([
            InlineKeyboardButton("✏️ Escribir", callback_data="q_w:%s:%d" % (short_key, qidx)),
        ])
        keyboard = InlineKeyboardMarkup(rows) if rows else None
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", reply_markup=keyboard)
            logging.info("_send_question_message: sent question %s qidx=%d with %d options", short_key, qidx, len(q_options))
        except Exception as e:
            logging.error("_send_question_message: send_message error qidx=%d: %s", qidx, str(e))
            try:
                await bot.send_message(chat_id=chat_id, text=msg, reply_markup=keyboard)
                logging.info("_send_question_message: sent without parse_mode qidx=%d", qidx)
            except Exception as e2:
                logging.error("_send_question_message: send_message fallback error: %s", str(e2))
    await _restore_live_last(bot, chat_id)


async def _answer_question(bot, chat_id: int, req_id: str, qidx: int, answer_text: str):
    info = known_questions.get(req_id, {})
    num = info.get("num", max(qidx + 1, 1))
    short_key = info.get("short", "")
    questions = info.get("questions", [])
    session_id = info.get("session_id") or user_sessions.get(chat_id)
    if session_id:
        turn_owner[session_id] = chat_id
        save_turn_owner(turn_owner)
    answers = [[] for _ in range(num)]
    answers[qidx] = [answer_text.strip()]

    target_id = req_id
    if not target_id.startswith("que_"):
        target_id = await _resolve_question_request_id(session_id, questions)

    replied = False
    if target_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = OPENCODE_URL + "/question/" + target_id + "/reply"
                r = await client.post(url, json={"answers": answers}, auth=get_auth())
                if r.status_code == 200:
                    replied = True
                else:
                    v2 = OPENCODE_URL + "/api/session/" + session_id + "/question/" + target_id + "/reply"
                    r2 = await client.post(v2, json={"answers": answers}, auth=get_auth())
                    if r2.status_code in (200, 201, 204):
                        replied = True
                    else:
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text="Error al responder: " + str(r2.status_code) + " " + str(r2.text)[:200],
                            )
                        except Exception:
                            pass
        except Exception as e:
            logging.error("question_reply: " + str(e))
    else:
        logging.info("question_reply: sin requestID pendiente (desktop?) para %s", req_id)

    if not replied and session_id:
        q_text = ""
        if questions and isinstance(questions[0], dict):
            q_text = (questions[0].get("question") or "").strip()
        elif questions:
            q_text = str(questions[0]).strip()
        reply_text = answer_text.strip()
        if q_text:
            send_text = "Respondiendo a tu pregunta «%s»: %s" % (q_text, reply_text)
        else:
            send_text = reply_text
        try:
            await _process_opencode_message(bot, chat_id, send_text)
        except Exception as e:
            logging.error("question_fallback process: " + str(e))
            try:
                await bot.send_message(chat_id=chat_id, text="Error al enviar la respuesta: " + str(e))
            except Exception:
                pass

    if req_id in known_questions:
        known_questions.pop(req_id, None)
    if short_key:
        _short_to_call.pop(short_key, None)


async def _poll_messages_and_permissions(
    poll_client: httpx.AsyncClient, bot, session_id: str, chat_id: int,
    known_ids: set, thinking_msg_id: int, max_iterations: int = 60,
):
    for _ in range(max_iterations):
        await asyncio.sleep(3)
        try:
            r_perm = await poll_client.get(OPENCODE_URL + "/permission", auth=get_auth())
            if r_perm.status_code == 200:
                for perm in r_perm.json():
                    await _send_permission_message(bot, chat_id, perm)
        except Exception:
            pass
        try:
            r_q = await poll_client.get(OPENCODE_URL + "/question", auth=get_auth())
            if r_q.status_code == 200:
                qs = r_q.json()
                logging.info("poll_questions found %d pending", len(qs))
                for qreq in qs:
                    if qreq.get("sessionID") == session_id:
                        logging.info("poll_questions: sending question %s to chat %d", qreq.get("id"), chat_id)
                        await _send_question_message(bot, chat_id, qreq)
            else:
                logging.info("poll_questions status %d", r_q.status_code)
        except Exception as e:
            logging.error("poll_questions error: %s", str(e))
        try:
            r_poll = await poll_client.get(
                OPENCODE_URL + "/session/" + session_id + "/message", auth=get_auth(),
            )
            if r_poll.status_code != 200:
                continue
            for m in r_poll.json():
                mid = m["info"].get("id", "")
                if mid in known_ids:
                    continue
                role = m["info"].get("role", "")
                finish = m["info"].get("finish")
                logging.info("poll found msg id=%s role=%s finish=%s", mid, role, finish)
                if role != "assistant":
                    known_ids.add(mid)
                    continue
                error = m["info"].get("error")
                if error:
                    name = error.get("name", "Error") if isinstance(error, dict) else "Error"
                    error_data = error.get("data", {}) if isinstance(error, dict) else {}
                    detail = error_data.get("message", "") if isinstance(error_data, dict) else str(error_data)
                    logging.error("OpenCode assistant message %s failed: %s %s", mid, name, detail)
                    if name == "MessageAbortedError":
                        ts = _intentional_aborts.get(session_id, 0)
                        if ts and (time.time() - ts) < 60:
                            known_ids.add(mid)
                            continue
                        known_ids.add(mid)
                        raise SessionBrokenError(
                            "OpenCode aborto este turno antes de generar una respuesta. "
                            "OpenCode solo devolvio 'Aborted'; no expuso el motivo interno."
                        )
                    known_ids.add(mid)
                    detail = (detail or "").strip()
                    if detail:
                        raise SessionBrokenError(
                            "OpenCode (%s): %s" % (name, detail[:700])
                        )
                    raise SessionBrokenError("OpenCode (%s): sin detalle del error." % name)

                for part in m.get("parts", []):
                    if part.get("type") == "tool" and part.get("tool") == "question":
                        state = part.get("state", {})
                        status = state.get("status", "")
                        if status == "running":
                            await _send_question_buttons_from_tool(bot, chat_id, session_id, part)

                completed = _count_completed_tools(m)
                already_sent = sent_tool_count.get(mid, 0)
                if completed > already_sent:
                    await _send_tools_collapsed(bot, chat_id, m, already_sent)
                    sent_tool_count[mid] = completed
                    await _restore_live_last(bot, chat_id)
                has_pending = _has_empty_tool_parts(m)
                msg_finish = m["info"].get("finish")
                if not has_pending and msg_finish == "stop":
                    skip_r = mid in sent_reasoning_ids
                    formatted = _format_full_response(m, skip_reasoning=skip_r, chat_id=chat_id)
                    logging.info("poll text candidate id=%s formatted_len=%d is_placeholder=%s", mid, len(formatted), formatted == "(sin respuesta de texto)")
                    if formatted != "(sin respuesta de texto)":
                        known_ids.add(mid)
                        sent_tool_count.pop(mid, None)
                        sent_reasoning_ids.discard(mid)
                        return (mid, formatted)
        except SessionBrokenError:
            raise
        except Exception as e:
            logging.error("poll error: %s", str(e))
            pass
    return None


async def _process_opencode_message(bot, chat_id: int, text: str):
    poll_client = httpx.AsyncClient(timeout=15.0)
    post_client = httpx.AsyncClient(timeout=600.0)
    thinking_msg = None
    try:
        if chat_id not in user_sessions:
            async with httpx.AsyncClient(timeout=30.0) as tmp:
                await _create_new_session(tmp, chat_id)

        session_id = user_sessions[chat_id]
        turn_owner[session_id] = chat_id
        save_turn_owner(turn_owner)

        try:
            r_msgs = await poll_client.get(
                OPENCODE_URL + "/session/" + session_id + "/message",
                auth=get_auth(), timeout=10.0,
            )
            r_msgs.raise_for_status()
        except Exception:
            raise SessionBrokenError("Error al consultar sesion")

        before_msgs = r_msgs.json()
        known_ids = {m["info"]["id"] for m in before_msgs}

        needs_abort = False
        if before_msgs:
            last = before_msgs[-1]
            last_info = last.get("info", {})
            last_role = last_info.get("role", "")
            last_finish = last_info.get("finish")
            if last_role == "assistant" and not last_info.get("error") and last_finish in ("tool-calls", None, ""):
                needs_abort = True
            if last_role == "user" and last_finish is None:
                needs_abort = True
            last_5 = before_msgs[-5:]
            consecutive_user = 0
            for m in reversed(last_5):
                if m.get("info", {}).get("role") != "user":
                    break
                consecutive_user += 1
            if consecutive_user >= 2:
                needs_abort = True

        if needs_abort and chat_id not in skip_broken_check:
            _intentional_aborts[session_id] = time.time()
            try:
                await poll_client.post(
                    OPENCODE_URL + "/session/" + session_id + "/abort",
                    auth=get_auth(), timeout=5.0,
                )
                await asyncio.sleep(2)
            except Exception:
                pass
            try:
                r_refresh = await poll_client.get(
                    OPENCODE_URL + "/session/" + session_id + "/message",
                    auth=get_auth(), timeout=10.0,
                )
                if r_refresh.status_code == 200:
                    for m in r_refresh.json():
                        mid2 = m.get("info", {}).get("id", "")
                        if mid2:
                            known_ids.add(mid2)
            except Exception:
                pass

        skip_broken_check.discard(chat_id)

        thinking_msg = await bot.send_message(chat_id=chat_id, text="Pensando...")
        active_thinking[chat_id] = thinking_msg.message_id
        thinking_started[chat_id] = time.time()
        last_activity[chat_id] = time.time()

        if chat_id in chat_model:
            model_payload = dict(chat_model[chat_id])
        else:
            model_payload = dict(DEFAULT_MODEL)
        post_body = {"parts": [{"type": "text", "text": text}]}
        if model_payload:
            post_body["model"] = model_payload
        logging.info("[%d] usando modelo %s", chat_id, model_payload)

        post_task = asyncio.create_task(
            _post_checked(
                post_client,
                OPENCODE_URL + "/session/" + session_id + "/message",
                json=post_body,
                auth=get_auth(), timeout=600.0,
            )
        )

        start_time = asyncio.get_event_loop().time()

        while not post_task.done():
            await asyncio.sleep(3)
            elapsed = int(asyncio.get_event_loop().time() - start_time)

            result = await _poll_messages_and_permissions(
                poll_client, bot, session_id, chat_id, known_ids,
                thinking_msg.message_id, max_iterations=1,
            )
            if result:
                post_task.cancel()
                try:
                    await post_task
                except Exception:
                    pass
                final_mid, final_text = result
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=thinking_msg.message_id)
                except Exception:
                    pass
                await _deliver_final(bot, chat_id, final_mid, final_text)
                active_thinking.pop(chat_id, None)
                return

            if time.time() - last_activity.get(chat_id, start_time) >= 600:
                post_task.cancel()
                try:
                    await post_task
                except Exception:
                    pass
                try:
                    await poll_client.post(
                        OPENCODE_URL + "/session/" + session_id + "/abort",
                        auth=get_auth(), timeout=5.0,
                    )
                except Exception:
                    pass
                logging.info("[%d] sin actividad 600s, run abortado", chat_id)
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=thinking_msg.message_id)
                except Exception:
                    pass
                await bot.send_message(chat_id=chat_id, text="Timeout: opencode sin actividad en 600s. Run cancelado, puedes reintentar.")
                active_thinking.pop(chat_id, None)
                thinking_started.pop(chat_id, None)
                last_activity.pop(chat_id, None)
                return

        try:
            post_task.result()
        except asyncio.CancelledError:
            pass
        except httpx.ReadError:
            pass
        except httpx.ConnectError:
            await bot.send_message(chat_id=chat_id, text="Error: opencode apagado.")
            active_thinking.pop(chat_id, None)
            return
        except Exception as e:
            logging.exception("[" + str(chat_id) + "] POST error")
            err_text = _extract_http_error(e)
            await _send_error_to_tg(bot, chat_id, err_text, thinking_msg.message_id)
            active_thinking.pop(chat_id, None)
            return

        while True:
            if time.time() - last_activity.get(chat_id, start_time) >= 600:
                try:
                    await poll_client.post(
                        OPENCODE_URL + "/session/" + session_id + "/abort",
                        auth=get_auth(), timeout=5.0,
                    )
                except Exception:
                    pass
                logging.info("[%d] sin actividad 600s, run abortado (fase espera)", chat_id)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=thinking_msg.message_id,
                        text="No se recibio actividad de opencode en 600s. Run cancelado, puedes reintentar.",
                    )
                except Exception:
                    pass
                active_thinking.pop(chat_id, None)
                thinking_started.pop(chat_id, None)
                last_activity.pop(chat_id, None)
                return

            result = await _poll_messages_and_permissions(
                poll_client, bot, session_id, chat_id, known_ids,
                thinking_msg.message_id, max_iterations=1,
            )
            if result:
                final_mid, final_text = result
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=thinking_msg.message_id)
                except Exception:
                    pass
                await _deliver_final(bot, chat_id, final_mid, final_text)
                active_thinking.pop(chat_id, None)
                thinking_started.pop(chat_id, None)
                last_activity.pop(chat_id, None)
                _activity_sig.pop(chat_id, None)
                return

    except SessionBrokenError as e:
        post_task = locals().get("post_task")
        if post_task and not post_task.done():
            post_task.cancel()
            try:
                await post_task
            except BaseException:
                pass
        try:
            if thinking_msg:
                await bot.delete_message(chat_id=chat_id, message_id=thinking_msg.message_id)
        except Exception:
            pass
        await bot.send_message(chat_id=chat_id, text="Error: " + str(e))
        active_thinking.pop(chat_id, None)
        thinking_started.pop(chat_id, None)
        last_activity.pop(chat_id, None)
    except asyncio.CancelledError:
        active_thinking.pop(chat_id, None)
    except Exception as e:
        logging.exception("[" + str(chat_id) + "] Exception")
        try:
            await bot.send_message(chat_id=chat_id, text="Error: " + type(e).__name__ + ": " + str(e)[:200])
        except Exception:
            pass
        active_thinking.pop(chat_id, None)
    finally:
        try:
            await poll_client.aclose()
        except Exception:
            pass
        try:
            await post_client.aclose()
        except Exception:
            pass


async def _handle_document(update: Update, bot) -> str | None:
    doc = update.message.document
    caption = (update.message.caption or "").strip()
    try:
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
        file_name = (doc.file_name or ("archivo_" + doc.file_id)).replace("\\", "_").replace("/", "_")
        path = os.path.join(ATTACHMENTS_DIR, file_name)
        f = await bot.get_file(doc.file_id)
        await f.download_to_drive(path)
    except Exception as e:
        logging.exception("[" + str(update.effective_chat.id) + "] descarga fichero error")
        try:
            await update.message.reply_text("Error al descargar el archivo: " + str(e)[:200])
        except Exception:
            pass
        return None
    size = getattr(doc, "file_size", None)
    note = " (%d bytes)" % size if size else ""
    msg = f'El usuario envio el archivo "{file_name}"{note}.\nGuardado en: {path}'
    if caption:
        msg += "\nInstruccion del usuario: " + caption
    return msg


async def _handle_photo(update: Update, bot) -> str | None:
    photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip()
    try:
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
        stamp = update.message.date.strftime("%Y%m%d_%H%M%S")
        uq = (photo.file_unique_id or "")[:12]
        path = os.path.join(ATTACHMENTS_DIR, "imagen_%s_%s.jpg" % (stamp, uq))
        f = await bot.get_file(photo.file_id)
        await f.download_to_drive(path)
    except Exception as e:
        logging.exception("[" + str(update.effective_chat.id) + "] descarga imagen error")
        try:
            await update.message.reply_text("Error al descargar la imagen: " + str(e)[:200])
        except Exception:
            pass
        return None
    msg = 'El usuario envio la imagen "{}".\nGuardado en: {}'.format(os.path.basename(path), path)
    if caption:
        msg += "\nInstruccion del usuario: " + caption
    return msg


async def recibir_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bot = context.bot
    is_doc = update.message.document is not None
    is_photo = bool(update.message.photo)
    if is_doc:
        await update.message.chat.send_action("upload_document")
        mensaje = await _handle_document(update, bot)
        if mensaje is None:
            return
    elif is_photo:
        await update.message.chat.send_action("upload_photo")
        mensaje = await _handle_photo(update, bot)
        if mensaje is None:
            return
    else:
        mensaje = (update.message.text or "").strip()

    is_attachment = is_doc or is_photo

    if (not is_attachment) and chat_id in pending_model:
        pending_model.discard(chat_id)
        models = _model_choice_cache.get(chat_id)
        if not models:
            await update.message.reply_text("Lista no disponible. Envía /model de nuevo.")
            return
        if mensaje.lower() == "cancelar":
            await update.message.reply_text("Cancelado.")
            return
        if not mensaje.isdigit():
            await update.message.reply_text("Numero invalido.")
            return
        idx = int(mensaje) - 1
        if idx < 0 or idx >= len(models):
            await update.message.reply_text("Numero fuera de rango.")
            return
        chosen = models[idx]
        chat_model[chat_id] = {"providerID": chosen["providerID"], "modelID": chosen["modelID"]}
        save_chat_models(chat_model)
        _model_choice_cache.pop(chat_id, None)
        await update.message.reply_text(
            f"Modelo cambiado a: {chosen['providerID']}/{chosen['modelID']} ({chosen['name']})"
        )
        return

    if (not is_attachment) and chat_id in pending_question_reply:
        req_id, qidx = pending_question_reply.pop(chat_id)
        await update.message.reply_text("Respuesta: " + mensaje)
        asyncio.create_task(_answer_question(bot, chat_id, req_id, qidx, mensaje))
        return

    if (not is_attachment) and chat_id in pending_unarchive:
        pending_unarchive.discard(chat_id)
        cache_file = os.path.join(os.path.dirname(__file__), "all_sessions_cache.json")
        if not os.path.exists(cache_file):
            await update.message.reply_text("Error: cache no encontrado. Usa /desarchivar de nuevo.")
            return
        with open(cache_file, "r") as f:
            sessions = json.load(f)
        if mensaje.lower() == "cancelar":
            await update.message.reply_text("Cancelado.")
            return
        if not mensaje.isdigit():
            await update.message.reply_text("Numero invalido.")
            return
        idx = int(mensaje) - 1
        if idx < 0 or idx >= len(sessions):
            await update.message.reply_text("Numero fuera de rango.")
            return
        selected = sessions[idx]
        archived_sessions.discard(selected["id"])
        save_archived(archived_sessions)
        await update.message.reply_text("Sesion '" + selected.get("title", selected["id"]) + "' desarchivada.")
        return

    if (not is_attachment) and chat_id in pending_archive:
        pending_archive.discard(chat_id)
        cache_file = os.path.join(os.path.dirname(__file__), "all_sessions_cache.json")
        if not os.path.exists(cache_file):
            await update.message.reply_text("Error: cache no encontrado. Usa /archivar de nuevo.")
            return
        with open(cache_file, "r") as f:
            sessions = json.load(f)
        if mensaje.lower() == "cancelar":
            await update.message.reply_text("Cancelado.")
            return
        if not mensaje.isdigit():
            await update.message.reply_text("Numero invalido.")
            return
        idx = int(mensaje) - 1
        if idx < 0 or idx >= len(sessions):
            await update.message.reply_text("Numero fuera de rango.")
            return
        selected = sessions[idx]
        archived_sessions.add(selected["id"])
        save_archived(archived_sessions)
        await update.message.reply_text("Sesion '" + selected.get("title", selected["id"]) + "' archivada.")
        return

    if (not is_attachment) and chat_id in pending_selection:
        pending_selection.discard(chat_id)
        cache_file = os.path.join(os.path.dirname(__file__), "all_sessions_cache.json")
        if not os.path.exists(cache_file):
            await update.message.reply_text("Error: cache no encontrado. Usa /sesiones de nuevo.")
            return
        with open(cache_file, "r") as f:
            sessions = json.load(f)
        if mensaje.lower() == "nueva":
            async with httpx.AsyncClient(timeout=30.0) as tmpc:
                new_id = await _create_new_session(tmpc, chat_id)
            skip_broken_check.add(chat_id)
            await update.message.reply_text("Sesion nueva creada y seleccionada.\nContinua hablando.")
            return
        elif mensaje.isdigit():
            idx = int(mensaje) - 1
            if 0 <= idx < len(sessions):
                selected = sessions[idx]
                selected_id = selected["id"]
                if selected.get("_desktop_only"):
                    await update.message.reply_text(
                        "Esa sesion solo existe en la app de escritorio; el bot no puede abrirla "
                        "porque el CLI de opencode no la conoce.\nUsala desde el escritorio o elige otra."
                    )
                    return
                shared_with = [cid for cid, sid in user_sessions.items() if sid == selected_id and cid != chat_id]
                user_sessions[chat_id] = selected_id
                save_sessions(user_sessions)
                skip_broken_check.add(chat_id)
                extra = ""
                if shared_with:
                    extra = "\n(Sesion compartida con %d chat(s) más)" % len(shared_with)
                await update.message.reply_text("Sesion '" + selected.get("title", selected_id) + "' seleccionada." + extra + "\nContinua hablando.")
                return
            else:
                await update.message.reply_text("Numero invalido.")
                return
        else:
            await update.message.reply_text("Respuesta no valida.")
            return

    await update.message.chat.send_action("typing")

    low = mensaje.lower()
    if "modelo eres" in low or "modelo usas" in low or "modelo estas usando" in low or \
       "que modelo" in low and "usas" in low or "what model" in low:
        session_id = user_sessions.get(chat_id)
        eff = None
        if chat_id in chat_model:
            eff = dict(chat_model[chat_id])
        elif session_id:
            eff = await get_session_model(session_id)
        if eff:
            await update.message.reply_text(
                f"Estoy usando: {eff['providerID']}/{eff['modelID']}"
            )
        else:
            await update.message.reply_text("No hay sesión activa todavía.")
        return

    if chat_id in active_tasks and not active_tasks[chat_id].done():
        active_tasks[chat_id].cancel()

    async def _safe_process():
        try:
            await _process_opencode_message(context.bot, chat_id, mensaje)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.exception("[" + str(chat_id) + "] Exception in task")
            try:
                await context.bot.send_message(chat_id=chat_id, text="Error: " + type(e).__name__ + ": " + str(e))
            except Exception:
                pass

    task = context.application.create_task(_safe_process())
    active_tasks[chat_id] = task


async def _poll_permissions(context: ContextTypes.DEFAULT_TYPE):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(OPENCODE_URL + "/permission", auth=get_auth())
            if r.status_code != 200:
                return
            permissions = r.json()
            if not isinstance(permissions, list):
                return
            for perm in permissions:
                req_id = perm.get("id")
                if not req_id or req_id in known_permissions:
                    continue
                known_permissions[req_id] = 0
                url = OPENCODE_URL + "/permission/" + req_id + "/reply"
                rp = await client.post(url, json={"reply": "always"}, auth=get_auth())
                if rp.status_code == 200:
                    logging.info("Auto-approved perm " + req_id + " (always)")
    except Exception as e:
        logging.error("poll_permissions: " + str(e))


async def _poll_questions(context: ContextTypes.DEFAULT_TYPE):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(OPENCODE_URL + "/question", auth=get_auth())
            if r.status_code != 200:
                return
            questions = r.json()
            if not isinstance(questions, list):
                return
            for qreq in questions:
                req_id = qreq.get("id")
                session_id = qreq.get("sessionID", "")
                if not req_id or req_id in known_questions:
                    continue
                target = turn_owner.get(session_id)
                if target not in user_sessions or user_sessions.get(target) != session_id:
                    target = None
                    for chat_id, user_session in user_sessions.items():
                        if user_session == session_id:
                            target = chat_id
                            break
                if target is not None:
                    logging.info("poll_questions: sending question %s to chat %d", req_id, target)
                    await _send_question_message(context.bot, target, qreq)
    except Exception as e:
        logging.error("poll_questions: " + str(e))


async def _activity_snapshot(session_id: str):
    try:
        async with httpx.AsyncClient(timeout=6.0) as ac:
            r = await ac.get(OPENCODE_URL + "/session/" + session_id + "/message", auth=get_auth())
            if r.status_code != 200:
                return "", False, ""
            msgs = r.json()
        if not msgs:
            return "", False, ""
        latest = None
        for m in reversed(msgs):
            if m.get("info", {}).get("role") == "assistant":
                latest = m
                break
        if not latest:
            return "", False, ""
        mid = latest.get("info", {}).get("id", "")
        finish = latest.get("info", {}).get("finish")
        running = False
        line = ""
        sig_parts = []
        for p in latest.get("parts", []):
            ptype = p.get("type", "")
            if ptype == "tool":
                state = p.get("state", {})
                inp = state.get("input", {})
                output = state.get("output", "")
                has_input = bool(inp) and (not isinstance(inp, dict) or any(inp.values()))
                has_output = bool(output) and str(output).strip()
                if has_input:
                    tool_name = p.get("tool", "?")
                    sig_parts.append(ptype + ":" + tool_name + ":" + str(len(str(output))))
                    if tool_name == "question":
                        line = _format_tool_short("question", inp)
                    elif not has_output:
                        running = True
                        line = _format_tool_short(tool_name, inp)
                    else:
                        line = "Hecho: " + _format_tool_short(tool_name, inp)
            elif ptype in ("text", "reasoning"):
                sig_parts.append(ptype + ":" + str(len(p.get("text", ""))))
        sig = mid + "|" + str(finish) + "|" + "|".join(sig_parts)
        if not line and finish in (None, ""):
            line = "Generando respuesta..."
        return line, running, sig
    except Exception:
        return "", False, ""


async def _heartbeat(context: ContextTypes.DEFAULT_TYPE):
    if not active_thinking:
        return
    now = time.time()
    for chat_id, msg_id in list(active_thinking.items()):
        if live_stream.get(chat_id):
            continue
        started = thinking_started.get(chat_id, now)
        elapsed = int(now - started)
        line = "⏳ Trabajando... (" + str(elapsed) + "s)"
        if elapsed >= 120:
            line = "⏳ Sigue trabajando... (" + str(elapsed) + "s)"
        session_id = user_sessions.get(chat_id, "")
        if session_id:
            act, running, sig = await _activity_snapshot(session_id)
            changed = bool(sig) and sig != _activity_sig.get(chat_id, "")
            if running or changed:
                last_activity[chat_id] = now
            _activity_sig[chat_id] = sig


async def _mirror_session_messages(context: ContextTypes.DEFAULT_TYPE):
    global _pending_rich_md
    bot = context.bot
    for chat_id, session_id in list(user_sessions.items()):
        shared = sum(1 for cid, sid in user_sessions.items() if sid == session_id) > 1
        owner = turn_owner.get(session_id)
        if shared and owner != chat_id:
            continue
        try:
            async with httpx.AsyncClient(timeout=8.0) as ac:
                r = await ac.get(OPENCODE_URL + "/session/" + session_id + "/message", auth=get_auth())
                if r.status_code != 200:
                    continue
                msgs = r.json()
        except Exception:
            continue
        if session_id not in primed_sessions:
            for mms in reversed(msgs):
                info = mms.get("info", {})
                shown_final_ids.add(info.get("id", ""))
            for mms in reversed(msgs):
                info = mms.get("info", {})
                if info.get("role") == "assistant" and info.get("finish") == "stop":
                    if _plain_text_of(mms) and info.get("id", "") not in delivered_finals:
                        shown_final_ids.discard(info.get("id", ""))
                    break
            recovered = False
            for mms in reversed(msgs):
                info = mms.get("info", {})
                if info.get("role") != "assistant":
                    continue
                if info.get("finish") == "stop":
                    if recovered:
                        continue
                    break
                txt = _plain_text_of(mms)
                if txt:
                    mid = info.get("id", "")
                    live_mids.add(mid)
                    try:
                        await bot.send_message(chat_id=chat_id, text="↪️ " + txt[:3500])
                    except Exception:
                        pass
                    recovered = True
                    break
                break
            primed_sessions.add(session_id)
            continue
        live = live_stream.get(chat_id)
        for m in msgs:
            mid = m.get("info", {}).get("id", "")
            if not mid:
                continue
            role = m.get("info", {}).get("role", "")
            finish = m.get("info", {}).get("finish")
            if live and live.get("mid") == mid and finish == "stop":
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=live["msg_id"])
                except Exception:
                    pass
                live_stream.pop(chat_id, None)
                live = None
            if role != "assistant":
                continue
            if mid in shown_final_ids:
                continue
            completed = _count_completed_tools(m)
            already = sent_tool_count.get(mid, 0)
            if completed > already:
                await _send_tools_collapsed(bot, chat_id, m, already)
                sent_tool_count[mid] = completed
                await _restore_live_last(bot, chat_id)
            for part in m.get("parts", []):
                if part.get("type") == "tool" and part.get("tool") == "question":
                    st = part.get("state", {})
                    status = st.get("status", "")
                    if status == "running":
                        await _send_question_buttons_from_tool(bot, chat_id, session_id, part)
                        await _restore_live_last(bot, chat_id)
            told = mid in live_mids
            if finish != "stop":
                if not ALLOW_LIVE:
                    txt = _plain_text_of(m)
                    if txt and txt not in _shown_inter_texts:
                        _shown_inter_texts.add(txt)
                        try:
                            await bot.send_message(chat_id=chat_id, text=txt)
                        except Exception:
                            pass
                    continue
                if not told:
                    txt = _plain_text_of(m)
                    if txt:
                        live_mids.add(mid)
                        try:
                            nb = await bot.send_message(chat_id=chat_id, text="⏳ " + txt[:3500])
                        except Exception:
                            nb = None
                        if nb:
                            if live:
                                try:
                                    await bot.delete_message(chat_id=chat_id, message_id=live["msg_id"])
                                except Exception:
                                    pass
                            live_stream[chat_id] = {"mid": mid, "msg_id": nb.message_id, "len": len(txt), "text": "⏳ " + txt[:3500]}
                            live = live_stream[chat_id]
                        continue
                    continue
                live = live_stream.get(chat_id)
                if live and live.get("mid") == mid:
                    txt = _plain_text_of(m)
                    if txt and len(txt) != live.get("len"):
                        try:
                            await bot.edit_message_text(chat_id=chat_id, message_id=live["msg_id"], text="⏳ " + txt[:3500])
                            live["len"] = len(txt)
                            live["text"] = "⏳ " + txt[:3500]
                        except Exception:
                            pass
            has_pending = _has_empty_tool_parts(m)
            if not has_pending and finish == "stop" and mid not in shown_final_ids:
                skip_r = mid in sent_reasoning_ids
                formatted = _format_full_response(m, skip_reasoning=skip_r, chat_id=chat_id)
                if formatted != "(sin respuesta de texto)":
                    await _deliver_final(bot, chat_id, mid, formatted)


async def _handle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("perm_"):
        return
    action, req_id = data.split(":", 1)
    reply_map = {"perm_once": "once", "perm_always": "always", "perm_reject": "reject"}
    label_map = {"perm_once": "UNA VEZ", "perm_always": "SIEMPRE", "perm_reject": "RECHAZADO"}
    reply = reply_map.get(action, "reject")
    label = label_map.get(action, action)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = OPENCODE_URL + "/permission/" + req_id + "/reply"
            r = await client.post(url, json={"reply": reply}, auth=get_auth())
            if r.status_code == 200:
                await query.edit_message_text(text=query.message.text + "\n\n=> " + label)
            else:
                await query.edit_message_text(text=query.message.text + "\n\n=> Error " + str(r.status_code) + ": " + r.text[:200])
    except Exception as e:
        logging.error("perm_callback: " + str(e))
        try:
            await query.edit_message_text(text=query.message.text + "\n\n=> Error: " + str(e))
        except Exception:
            pass
    known_permissions.pop(req_id, None)


async def _handle_question_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("q_"):
        return
    try:
        parts = data.split(":")
        action = parts[0]
        short_key = parts[1]
        chat_id = query.message.chat_id
        call_id = _short_to_call.get(short_key)
        if not call_id:
            return

        if action == "q_a":
            qidx = int(parts[2])
            opt_idx = int(parts[3])
            q_info = known_questions.get(call_id, {})
            questions = q_info.get("questions", [])
            label = str(opt_idx)
            if questions and qidx < len(questions):
                opts = questions[qidx].get("options", [])
                if opt_idx < len(opts):
                    opt = opts[opt_idx]
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            try:
                await query.edit_message_text(text=query.message.text + "\n\n=> " + label)
            except Exception:
                pass
            asyncio.create_task(_answer_question(context.bot, chat_id, call_id, qidx, label))
        elif action == "q_w":
            qidx = int(parts[2])
            pending_question_reply[chat_id] = (call_id, qidx)
            try:
                await query.edit_message_text(text=query.message.text + "\n\n✍️ Escribe tu respuesta...")
            except Exception:
                pass
            try:
                await context.bot.send_message(chat_id=chat_id, text="Mándame tu respuesta por mensaje.")
            except Exception:
                pass
        elif action == "q_reject":
            known_questions.pop(call_id, None)
            short_key = parts[1]
            _short_to_call.pop(short_key, None)
            try:
                await query.edit_message_text(text=query.message.text + "\n\n=> Descartada")
            except Exception:
                pass
    except Exception as e:
        logging.error("question_callback: " + str(e))


app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("sesiones", cmd_sesiones))
app.add_handler(CommandHandler("nueva", cmd_nueva))
app.add_handler(CommandHandler("archivar", cmd_archivar))
app.add_handler(CommandHandler("desarchivar", cmd_desarchivar))
app.add_handler(CommandHandler("model", cmd_model))
app.add_handler(CommandHandler("models", cmd_model))
app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL | filters.PHOTO, recibir_mensaje))
app.add_handler(CallbackQueryHandler(_handle_permission_callback, pattern="^perm_"))
app.add_handler(CallbackQueryHandler(_handle_question_callback, pattern="^q_"))
app.add_handler(CallbackQueryHandler(_handle_model_callback, pattern="^sm_"))
app.add_handler(CallbackQueryHandler(_handle_tool_toggle_callback, pattern="^tool_toggle$"))

job_queue = app.job_queue
if job_queue:
    job_queue.run_repeating(_poll_permissions, interval=3.0, first=1.0)
    job_queue.run_repeating(_poll_questions, interval=3.0, first=1.0)
    job_queue.run_repeating(_mirror_session_messages, interval=3.0, first=5.0)
    job_queue.run_repeating(_heartbeat, interval=5.0, first=5.0)
else:
    print("WARNING: job_queue no disponible. Permisos deshabilitados.")

print("Bot conectado a opencode. Esperando mensajes...")
try:
    app.run_polling(
        poll_interval=2.0,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
except Exception:
    logging.exception("run_polling terminado por excepcion")
    try:
        import traceback

        crash_path = os.path.join(os.path.dirname(__file__), "crash.log")
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write("\n===== CRASH " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
            f.write(traceback.format_exc())
    except Exception:
        pass
finally:
    _remove_pid()
