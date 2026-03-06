import logging
import asyncio
import base64
import json
import os
import sys
import shutil
import hashlib
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse, parse_qs, quote

# --- LOGGING PLUS VERBEUX (contrôlé par ENV) ---
# LOG_LEVEL: INFO|DEBUG (par défaut DEBUG)
# UVICORN_LOG_LEVEL: debug|info|warning...
# PW_VERBOSE=1 pour logs Playwright (requests/responses/console)
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()
UVICORN_LOG_LEVEL = os.getenv("UVICORN_LOG_LEVEL", "debug").lower()
PW_VERBOSE = os.getenv("PW_VERBOSE", "0") == "1"

# Sécurité secrets
# Si défini, seuls les secret_name commençant par ce préfixe sont autorisés.
# Ex: SECRET_NAME_PREFIX=PW_SECRET_
SECRET_NAME_PREFIX = os.getenv("SECRET_NAME_PREFIX", "")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp-manager")


# --- FILTRES ANTI "PING" / POLL (uvicorn access + SSE starlette) ---
class DropNoisyHttpLogs(logging.Filter):
    """
    Filtre les endpoints très bruyants:
    - /health, /ping: probes
    - /sse: SSE keepalive / reconnect
    - /messages/: transport MCP
    """
    NOISY_PATHS = ("/health", "/ping", "/sse", "/messages/", "/messages")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for p in self.NOISY_PATHS:
            if f'"GET {p} ' in msg or f'"POST {p} ' in msg:
                return False
        return True


class DropSseChunkLogs(logging.Filter):
    """
    Filtre les logs 'chunk: b'...' de sse_starlette.sse (ultra bruyant).
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg.startswith("chunk: b'") or msg.startswith('chunk: b"') or msg.startswith("chunk: "):
            return False
        return True


# Rendre uvicorn + mcp plus bavards
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "mcp.server", "mcp.server.lowlevel"):
    logging.getLogger(_name).setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))

# Appliquer les filtres
logging.getLogger("uvicorn.access").addFilter(DropNoisyHttpLogs())

# sse_starlette est souvent très bavard; on filtre les chunks
logging.getLogger("sse_starlette.sse").setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))
logging.getLogger("sse_starlette.sse").addFilter(DropSseChunkLogs())

# (Optionnel) starlette "général" peut aussi spammer selon config
logging.getLogger("starlette").setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))

try:
    import uvicorn
    from fastapi import FastAPI, Request
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    import mcp.types as types
    from playwright.async_api import async_playwright
except ImportError as e:
    logger.error(f"[FATAL] Dépendance manquante dans le Pod : {e}")
    raise


# --- ARCHITECTURE IMMUABLE (KIND/PVC/CDP) ---
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/app/downloads")
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# Exemple: ws://browserless.n8n-prod.svc.cluster.local:3000
BROWSERLESS_URL = os.getenv(
    "BROWSERLESS_URL",
    "ws://browserless.n8n-prod.svc.cluster.local:3000",
)

# URL du pod (utile pour debug / multi-pods / sticky sessions)
# Exemple en k8s: http://playwright-wrapper.n8n-prod.svc.cluster.local:8080
POD_URL = os.getenv("POD_URL", "http://localhost:8080")

# Limites de chunk (base64 gonfle ~33%).
# 512KB brut -> ~682KB base64, généralement safe.
DEFAULT_CHUNK_SIZE = int(os.getenv("CHUNK_SIZE_BYTES", "524288"))

# Scout DOM "full": longueur max de text récupéré par élément (innerText)
DOM_TEXT_MAX = int(os.getenv("DOM_TEXT_MAX", "400"))

mcp_server = Server("playwright-tools")
sessions: dict[str, dict] = {}
pw_manager = None
sse_transport = SseServerTransport("/messages/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pw_manager
    logger.info("[SYSTEM] Start Playwright Engine and CDP Connection pool...")
    pw_manager = await async_playwright().start()
    yield
    logger.info("[SYSTEM] Shutdown : Fermeture propre de toutes les sessions...")
    for _s_id, data in list(sessions.items()):
        try:
            try:
                if "context" in data and data["context"]:
                    await data["context"].close()
            except Exception as e:
                logger.warning(f"[WARN] Erreur fermeture context {_s_id}: {e}")
            try:
                if "browser" in data and data["browser"]:
                    await data["browser"].close()
            except Exception as e:
                logger.warning(f"[WARN] Erreur fermeture browser {_s_id}: {e}")
        except Exception as e:
            logger.warning(f"[WARN] Erreur lors de la fermeture de session {_s_id}: {e}")

    await pw_manager.stop()


app = FastAPI(title="n8n-Persistent-Scout", lifespan=lifespan)


# --- UTILITAIRES ---
def guess_mime_type(filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".json"):
        return "application/json"
    if fn.endswith(".pdf"):
        return "application/pdf"
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith(".jpg") or fn.endswith(".jpeg"):
        return "image/jpeg"
    if fn.endswith(".txt"):
        return "text/plain"
    if fn.endswith(".html") or fn.endswith(".htm"):
        return "text/html"
    return "application/octet-stream"


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_json_text(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def parse_filename_from_content_disposition(cd: str) -> str | None:
    if not cd:
        return None
    cd_low = cd.lower()
    if "filename=" not in cd_low:
        return None
    part = cd.split("filename=", 1)[-1].strip()
    if ";" in part:
        part = part.split(";", 1)[0].strip()
    return part.strip().strip('"').strip("'")


def is_target_closed_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "has been closed" in msg
        or "target page" in msg
        or "context or browser has been closed" in msg
        or "browser has been closed" in msg
        or "target closed" in msg
    )


async def close_and_forget_session(session_id: str):
    data = sessions.pop(session_id, None)
    if not data:
        return
    try:
        if data.get("context"):
            await data["context"].close()
    except Exception:
        pass
    try:
        if data.get("browser"):
            await data["browser"].close()
    except Exception:
        pass


def parse_query_param_from_url(url: str, key: str) -> str | None:
    try:
        q = parse_qs(urlparse(url).query)
        v = q.get(key)
        if not v:
            return None
        return v[0]
    except Exception:
        return None


def wiki_pdf_rest_url(current_url: str, page_title: str) -> str:
    """
    Construit: https://<host>/api/rest_v1/page/pdf/<TITLE>
    - page_title doit être le "Title" de MediaWiki (underscores ok)
    - on URL-encode au niveau du chemin
    """
    p = urlparse(current_url)
    origin = f"{p.scheme}://{p.netloc}"
    return f"{origin}/api/rest_v1/page/pdf/{quote(page_title, safe='')}"


async def wait_for_selector_soft(page, selector: str, timeout_ms: int = 15000) -> bool:
    """
    Attente robuste pour les sites hydratés (Vector/Wikipedia, SPA, etc.)
    - True si le sélecteur apparaît (attached), False si timeout.
    """
    try:
        await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        return True
    except Exception:
        return False


def enrich_payload(payload: dict, session_id: str | None, page=None) -> dict:
    """
    Ajoute systématiquement:
    - session_id (celui donné par n8n)
    - pod_url (env POD_URL)
    - page_url (si disponible)
    """
    enriched = dict(payload)
    enriched["session_id"] = session_id
    enriched["pod_url"] = POD_URL
    if page is not None:
        try:
            enriched["page_url"] = page.url
        except Exception:
            enriched["page_url"] = None
    return enriched


def mcp_text(payload: dict, session_id: str | None, page=None):
    return [types.TextContent(type="text", text=safe_json_text(enrich_payload(payload, session_id, page)))]


def get_origin(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return None
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return None


def resolve_secret_from_env(secret_name: str) -> str:
    """
    Lit un secret depuis les variables d'environnement du conteneur.
    Ne log jamais la valeur.

    Sécurité optionnelle:
    - si SECRET_NAME_PREFIX est défini, le nom doit commencer par ce préfixe
    """
    if not secret_name:
        raise ValueError("secret_name is required")

    if SECRET_NAME_PREFIX and not secret_name.startswith(SECRET_NAME_PREFIX):
        raise ValueError(
            f"secret_name '{secret_name}' is not allowed; it must start with prefix '{SECRET_NAME_PREFIX}'"
        )

    value = os.getenv(secret_name)
    if value is None:
        raise ValueError(f"Secret not found in environment: {secret_name}")

    if value == "":
        raise ValueError(f"Secret is empty: {secret_name}")

    return value


# --- SCRIPTS DOM ---
def dom_extractor_script():
    return """
    (maxLen) => {
        const textLimit = Number(maxLen || 200);

        const normalizeClass = (v) => {
            try {
                if (typeof v === 'string') return v || null;
                if (v && typeof v.baseVal === 'string') return v.baseVal || null;
                return String(v || '') || null;
            } catch(e) {
                return null;
            }
        };

        const safeAttr = (el, name) => {
            try { return el.getAttribute(name); } catch(e) { return null; }
        };

        const safeProp = (fn) => {
            try { return fn(); } catch(e) { return null; }
        };

        const elements = document.querySelectorAll('*');

        return Array.from(elements).map((el, index) => {
            const rect = safeProp(() => el.getBoundingClientRect()) || { width: 0, height: 0, top: 0, left: 0 };

            const href = safeProp(() => el.href || null);
            const src = safeProp(() => el.src || null);
            const data = safeProp(() => el.data || null);
            const action = safeProp(() => el.action || null);
            const method = safeProp(() => el.method || null);
            const value = safeProp(() => (el.value !== undefined ? el.value : null));
            const required = safeProp(() => (el.required === true));
            const checked = safeProp(() => (el.checked === true));
            const disabled = safeProp(() => (el.disabled === true));
            const selected = safeProp(() => (el.selected === true));

            let text = '';
            try {
                text = (el.innerText || el.textContent || '').trim().substring(0, textLimit);
            } catch(e) {
                text = '';
            }

            let html = '';
            try {
                html = (el.outerHTML || '').substring(0, Math.min(textLimit * 3, 2000));
            } catch(e) {
                html = '';
            }

            return {
                index: index,
                tag: safeProp(() => el.tagName.toLowerCase()) || null,
                id: el.id || null,
                name: safeAttr(el, 'name'),
                type: safeAttr(el, 'type'),
                role: safeAttr(el, 'role'),
                title: safeAttr(el, 'title'),
                placeholder: safeAttr(el, 'placeholder'),
                value: value,
                required: required,
                checked: checked,
                selected: selected,
                disabled: disabled,
                class: normalizeClass(el.className),
                text: text,
                html: html,
                href: href,
                src: src,
                data: data,
                action: action,
                method: method,
                target: safeAttr(el, 'target'),
                rel: safeAttr(el, 'rel'),
                download: safeAttr(el, 'download'),
                ariaExpanded: safeAttr(el, 'aria-expanded'),
                ariaLabel: safeAttr(el, 'aria-label'),
                frameSrc: (safeProp(() => el.tagName.toLowerCase()) === 'iframe') ? src : null,
                isVisible: (rect.width > 0 && rect.height > 0),
                rect: {
                    w: rect.width || 0,
                    h: rect.height || 0,
                    t: rect.top || 0,
                    l: rect.left || 0
                }
            };
        }).filter(el => !['script', 'style', 'meta', 'link', 'noscript'].includes(el.tag));
    }
    """


async def extract_dom_from_target(target, text_max_len: int):
    return await target.evaluate(dom_extractor_script(), int(text_max_len))


async def extract_deep_dom(page):
    logger.info("[DOM] Execution du RAW Scout Report (Extraction exhaustive - light)...")
    return await extract_dom_from_target(page, 120)


async def extract_deep_dom_full(page, text_max_len: int):
    logger.info("[DOM] Execution du RAW Scout Report (Extraction exhaustive - FULL)...")
    return await extract_dom_from_target(page, text_max_len)


async def extract_frames_report(page, text_max_len: int = 200, include_dom: bool = True):
    """
    Inspecte toutes les frames Playwright, y compris les iframes.
    Retourne:
    - meta des frames
    - URL réelle
    - parent frame
    - DOM interne (si include_dom=True)
    """
    logger.info(f"[FRAMES] Extraction frames include_dom={include_dom} text_max_len={text_max_len}")

    page_origin = get_origin(page.url)
    page_frames = page.frames
    frame_reports = []

    for idx, frame in enumerate(page_frames):
        frame_url = None
        frame_name = None
        frame_title = None
        frame_origin = None
        frame_parent_url = None
        same_origin = None
        dom = None
        dom_error = None

        try:
            frame_url = frame.url
        except Exception as e:
            frame_url = f"ERROR:{e}"

        try:
            frame_name = frame.name
        except Exception:
            frame_name = None

        try:
            frame_origin = get_origin(frame_url)
            same_origin = (frame_origin == page_origin) if frame_origin and page_origin else None
        except Exception:
            same_origin = None

        try:
            parent = frame.parent_frame
            if parent:
                frame_parent_url = parent.url
        except Exception:
            frame_parent_url = None

        try:
            frame_title = await frame.title()
        except Exception:
            frame_title = None

        if include_dom:
            try:
                dom = await extract_dom_from_target(frame, text_max_len)
            except Exception as e:
                dom_error = str(e)

        frame_reports.append({
            "frame_index": idx,
            "name": frame_name,
            "url": frame_url,
            "origin": frame_origin,
            "same_origin_as_page": same_origin,
            "title": frame_title,
            "parent_url": frame_parent_url,
            "is_main_frame": (frame == page.main_frame),
            "dom_count": len(dom) if isinstance(dom, list) else None,
            "dom_error": dom_error,
            "elements": dom,
        })

    return {
        "page_url": page.url,
        "page_origin": page_origin,
        "frame_count": len(frame_reports),
        "frames": frame_reports,
    }


def attach_verbose_playwright_listeners(page):
    page.on("console", lambda msg: logger.debug(f"[PW:console] {msg.type}: {msg.text}"))
    page.on("pageerror", lambda err: logger.debug(f"[PW:pageerror] {err}"))
    page.on("request", lambda req: logger.debug(f"[PW:request] {req.method} {req.url}"))
    page.on("response", lambda res: logger.debug(f"[PW:response] {res.status} {res.url}"))
    page.on("requestfailed", lambda req: logger.debug(f"[PW:requestfailed] {req.method} {req.url} - {req.failure}"))


async def get_or_create_session(session_id: str):
    # Réutilisation si possible + test actif (important CDP/browserless)
    if session_id in sessions:
        data = sessions[session_id]
        try:
            if data["browser"].is_connected() and not data["page"].is_closed():
                await data["page"].evaluate("() => 1")
                return data["page"]
        except Exception:
            await close_and_forget_session(session_id)

    logger.info(f"[CDP] Creation d'une session DESKTOP HD (1920x1080) : {session_id}")
    logger.debug(f"[CDP] BROWSERLESS_URL={BROWSERLESS_URL}")

    browser = await pw_manager.chromium.connect_over_cdp(BROWSERLESS_URL)

    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        accept_downloads=True,
    )

    page = await context.new_page()
    page.set_default_timeout(30000)

    if PW_VERBOSE:
        logger.info("[PW] Verbose listeners enabled (PW_VERBOSE=1)")
        attach_verbose_playwright_listeners(page)

    sessions[session_id] = {"context": context, "page": page, "browser": browser}
    return page


@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    s_id = {"session_id": {"type": "string", "description": "ID de session persistante"}}
    return [
        types.Tool(
            name="navigate",
            description="Navigation + RAW Scout DOM (light) inline + résumé des frames",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}, **s_id},
                "required": ["url", "session_id"],
            },
        ),
        types.Tool(
            name="scout_dom",
            description="Analyse exhaustive des elements actuels (light) inline",
            inputSchema={"type": "object", "properties": {**s_id}, "required": ["session_id"]},
        ),
        types.Tool(
            name="scout_dom_full_to_file",
            description=(
                "Scout DOM FULL de la page courante -> écrit un JSON dans /app/downloads et renvoie meta + path + sha256. "
                "Utiliser read_file_chunk pour récupérer le fichier en base64 par morceaux."
            ),
            inputSchema={
                "type": "object",
                "properties": {"text_max_len": {"type": "integer", "description": "Longueur max innerText par élément (défaut env DOM_TEXT_MAX)"}, **s_id},
                "required": ["session_id"],
            },
        ),
        types.Tool(
            name="scout_frames",
            description=(
                "Inspecte toutes les frames/iframes de la page et renvoie inline un résumé: url, name, parent_url, "
                "same_origin, dom_count, erreurs éventuelles."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text_max_len": {"type": "integer"},
                    "include_dom": {"type": "boolean"},
                    **s_id
                },
                "required": ["session_id"],
            },
        ),
        types.Tool(
            name="scout_frames_to_file",
            description=(
                "Inspecte toutes les frames/iframes et écrit un JSON détaillé dans /app/downloads. "
                "Très utile pour voir le contenu des iframes. Utiliser read_file_chunk ensuite."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text_max_len": {"type": "integer"},
                    "include_dom": {"type": "boolean"},
                    **s_id
                },
                "required": ["session_id"],
            },
        ),
        types.Tool(
            name="click_element",
            description="Clic (Support 'idx:N') + Wait",
            inputSchema={
                "type": "object",
                "properties": {"selector": {"type": "string"}, "wait_for_selector": {"type": "string"}, **s_id},
                "required": ["selector", "session_id"],
            },
        ),
        types.Tool(
            name="fill_input",
            description="Saisie de texte en clair (robuste via locator.fill)",
            inputSchema={
                "type": "object",
                "properties": {"selector": {"type": "string"}, "value": {"type": "string"}, **s_id},
                "required": ["selector", "value", "session_id"],
            },
        ),
        types.Tool(
            name="fill_secret_input",
            description=(
                "Saisie d'un secret depuis une variable d'environnement du conteneur. "
                "On fournit secret_name au lieu de value. La valeur du secret n'est jamais renvoyée."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "secret_name": {"type": "string", "description": "Nom de la variable d'environnement à lire"},
                    **s_id
                },
                "required": ["selector", "secret_name", "session_id"],
            },
        ),
        types.Tool(
            name="download_file",
            description=(
                "Téléchargement via expect_download (Support 'idx:N'). "
                "Retourne meta + path + taille + sha256. Utiliser read_file_chunk pour récupérer le binaire."
            ),
            inputSchema={
                "type": "object",
                "properties": {"selector": {"type": "string"}, **s_id},
                "required": ["selector", "session_id"],
            },
        ),
        types.Tool(
            name="download_pdf_wikipedia",
            description=(
                "FIX Wikipedia (robuste): construit l'URL REST /api/rest_v1/page/pdf/<title> à partir du param 'page=' "
                "(ou fallback formulaire), télécharge via page.request.get, valide %PDF-."
            ),
            inputSchema={"type": "object", "properties": {**s_id}, "required": ["session_id"]},
        ),
        types.Tool(
            name="read_file_chunk",
            description="Lire un fichier local (path) en base64 par morceaux (chunk).",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "length": {"type": "integer"}, **s_id},
                "required": ["path", "offset", "length", "session_id"],
            },
        ),
        types.Tool(
            name="screenshot",
            description="Capture d'écran HD",
            inputSchema={"type": "object", "properties": {**s_id}, "required": ["session_id"]},
        ),
        types.Tool(
            name="purge_downloads",
            description="Vider /app/downloads",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "purge_downloads":
        logger.info(f"[PVC] Purge du dossier {DOWNLOAD_PATH}...")
        shutil.rmtree(DOWNLOAD_PATH, ignore_errors=True)
        os.makedirs(DOWNLOAD_PATH, exist_ok=True)
        return [types.TextContent(type="text", text="Stockage PVC nettoyé.")]

    session_id = arguments.get("session_id")

    # Retry once si "Target closed" (CDP/browserless instable)
    for attempt in (1, 2):
        page = await get_or_create_session(session_id)
        logger.info(f"[EXECUTE] {name} | SESSION: {session_id} | attempt={attempt} | URL: {page.url}")

        try:
            if name == "navigate":
                url = arguments["url"]
                logger.info(f"[NAV] goto {url}")
                await page.goto(url, wait_until="networkidle", timeout=60000)
                logger.debug(f"[NAV] landed {page.url}")

                # Petit délai anti-hydration
                await asyncio.sleep(0.6)

                dom = await extract_deep_dom(page)
                frames_report = await extract_frames_report(page, text_max_len=120, include_dom=False)

                payload = {
                    "url": page.url,
                    "scout_report": dom,
                    "frames_summary": {
                        "frame_count": frames_report["frame_count"],
                        "frames": [
                            {
                                "frame_index": f["frame_index"],
                                "name": f["name"],
                                "url": f["url"],
                                "origin": f["origin"],
                                "same_origin_as_page": f["same_origin_as_page"],
                                "parent_url": f["parent_url"],
                                "is_main_frame": f["is_main_frame"],
                            }
                            for f in frames_report["frames"]
                        ],
                    },
                }
                return mcp_text(payload, session_id, page)

            elif name == "scout_dom":
                dom = await extract_deep_dom(page)
                payload = {"elements": dom}
                return mcp_text(payload, session_id, page)

            elif name == "scout_dom_full_to_file":
                text_max_len = int(arguments.get("text_max_len") or DOM_TEXT_MAX)

                dom = await extract_deep_dom_full(page, text_max_len=text_max_len)

                ts = int(time.time())
                filename = f"scout_dom_full_{session_id}_{ts}.json"
                file_path = os.path.join(DOWNLOAD_PATH, filename)

                payload_file = {
                    "generated_at_unix": ts,
                    "page_url": page.url,
                    "text_max_len": text_max_len,
                    "elements": dom,
                }

                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(payload_file, f, ensure_ascii=False, separators=(",", ":"))

                size_bytes = os.path.getsize(file_path)
                sha256 = file_sha256(file_path)

                logger.info(f"[DOM_FULL] saved={file_path} size={size_bytes} sha256={sha256}")

                payload = {
                    "result": "OK",
                    "filename": filename,
                    "path": file_path,
                    "mimeType": "application/json",
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "chunk_recommended_bytes": DEFAULT_CHUNK_SIZE,
                }
                return mcp_text(payload, session_id, page)

            elif name == "scout_frames":
                text_max_len = int(arguments.get("text_max_len") or DOM_TEXT_MAX)
                include_dom = bool(arguments.get("include_dom", True))

                frames_report = await extract_frames_report(page, text_max_len=text_max_len, include_dom=include_dom)

                if not include_dom:
                    frames_report = {
                        **frames_report,
                        "frames": [
                            {
                                "frame_index": f["frame_index"],
                                "name": f["name"],
                                "url": f["url"],
                                "origin": f["origin"],
                                "same_origin_as_page": f["same_origin_as_page"],
                                "title": f["title"],
                                "parent_url": f["parent_url"],
                                "is_main_frame": f["is_main_frame"],
                                "dom_count": f["dom_count"],
                                "dom_error": f["dom_error"],
                            }
                            for f in frames_report["frames"]
                        ]
                    }

                return mcp_text(frames_report, session_id, page)

            elif name == "scout_frames_to_file":
                text_max_len = int(arguments.get("text_max_len") or DOM_TEXT_MAX)
                include_dom = bool(arguments.get("include_dom", True))

                frames_report = await extract_frames_report(page, text_max_len=text_max_len, include_dom=include_dom)

                ts = int(time.time())
                filename = f"scout_frames_{session_id}_{ts}.json"
                file_path = os.path.join(DOWNLOAD_PATH, filename)

                payload_file = {
                    "generated_at_unix": ts,
                    "page_url": page.url,
                    "text_max_len": text_max_len,
                    "include_dom": include_dom,
                    "frames_report": frames_report,
                }

                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(payload_file, f, ensure_ascii=False, separators=(",", ":"))

                size_bytes = os.path.getsize(file_path)
                sha256 = file_sha256(file_path)

                logger.info(f"[FRAMES_FULL] saved={file_path} size={size_bytes} sha256={sha256}")

                payload = {
                    "result": "OK",
                    "filename": filename,
                    "path": file_path,
                    "mimeType": "application/json",
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "chunk_recommended_bytes": DEFAULT_CHUNK_SIZE,
                    "frame_count": frames_report["frame_count"],
                }
                return mcp_text(payload, session_id, page)

            elif name == "click_element":
                selector = arguments["selector"]
                wait_for = arguments.get("wait_for_selector")
                logger.info(f"[CLICK] selector={selector} wait_for={wait_for}")

                if selector.startswith("idx:"):
                    index = int(selector.split(":")[1])
                    logger.debug(f"[CLICK] JS idx={index}")
                    ok = await page.evaluate(
                        """
                        (idx) => {
                            const el = document.querySelectorAll('*')[idx];
                            if (!el) return false;
                            el.scrollIntoView({block:'center', inline:'center'});
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            el.click();
                            return true;
                        }
                        """,
                        index,
                    )
                    if not ok:
                        return mcp_text({"result": "ERROR", "error": "idx element not found", "selector": selector}, session_id, page)

                else:
                    ok = await wait_for_selector_soft(page, selector, timeout_ms=15000)
                    if not ok:
                        return mcp_text(
                            {"result": "ERROR", "error": "Selector not found (timeout waiting attached)", "selector": selector, "current_url": page.url},
                            session_id,
                            page,
                        )

                    cnt = await page.locator(selector).count()
                    logger.debug(f"[CLICK] locator count={cnt}")
                    if cnt == 0:
                        return mcp_text(
                            {"result": "ERROR", "error": "Selector not found", "selector": selector, "current_url": page.url},
                            session_id,
                            page,
                        )

                    await page.locator(selector).first.scroll_into_view_if_needed(timeout=15000)
                    await page.click(selector, force=True, timeout=15000)

                if wait_for:
                    logger.info(f"[WAIT] selector={wait_for} state=attached")
                    await page.wait_for_selector(wait_for, state="attached", timeout=15000)
                else:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    await asyncio.sleep(0.6)

                new_dom = await extract_deep_dom(page)
                frames_report = await extract_frames_report(page, text_max_len=120, include_dom=False)

                payload = {
                    "action": "click",
                    "result": "OK",
                    "new_url": page.url,
                    "scout_report": new_dom,
                    "frames_summary": {
                        "frame_count": frames_report["frame_count"],
                        "frames": [
                            {
                                "frame_index": f["frame_index"],
                                "name": f["name"],
                                "url": f["url"],
                                "origin": f["origin"],
                                "same_origin_as_page": f["same_origin_as_page"],
                                "parent_url": f["parent_url"],
                                "is_main_frame": f["is_main_frame"],
                            }
                            for f in frames_report["frames"]
                        ],
                    },
                }
                return mcp_text(payload, session_id, page)

            elif name == "fill_input":
                selector = arguments["selector"]
                value = arguments["value"]

                # Ne pas logger la valeur
                logger.info(f"[FILL] selector={selector} mode=plain len(value)={len(value)}")

                ok = await wait_for_selector_soft(page, selector, timeout_ms=15000)
                if not ok:
                    return mcp_text(
                        {"result": "ERROR", "error": "Selector not found (timeout waiting attached)", "selector": selector, "current_url": page.url},
                        session_id,
                        page,
                    )

                await page.locator(selector).fill(value)

                payload = {
                    "action": "fill",
                    "result": "OK",
                    "mode": "plain",
                    "selector": selector,
                }
                return mcp_text(payload, session_id, page)

            elif name == "fill_secret_input":
                selector = arguments["selector"]
                secret_name = arguments["secret_name"]

                # Ne jamais logger la valeur du secret
                logger.info(f"[FILL_SECRET] selector={selector} secret_name={secret_name}")

                ok = await wait_for_selector_soft(page, selector, timeout_ms=15000)
                if not ok:
                    return mcp_text(
                        {
                            "result": "ERROR",
                            "error": "Selector not found (timeout waiting attached)",
                            "selector": selector,
                            "secret_name": secret_name,
                            "current_url": page.url,
                        },
                        session_id,
                        page,
                    )

                secret_value = resolve_secret_from_env(secret_name)
                await page.locator(selector).fill(secret_value)

                payload = {
                    "action": "fill",
                    "result": "OK",
                    "mode": "secret",
                    "selector": selector,
                    "secret_name": secret_name,
                }
                return mcp_text(payload, session_id, page)

            elif name == "download_file":
                selector = arguments["selector"]
                logger.info(f"[DOWNLOAD] expect_download selector={selector}")

                if selector.startswith("idx:"):
                    index = int(selector.split(":")[1])
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.evaluate(
                            """
                            (idx) => {
                                const el = document.querySelectorAll('*')[idx];
                                if (!el) throw new Error('Element idx introuvable: ' + idx);
                                el.scrollIntoView({block:'center', inline:'center'});
                                el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                el.click();
                            }
                            """,
                            index,
                        )
                else:
                    ok = await wait_for_selector_soft(page, selector, timeout_ms=15000)
                    if not ok:
                        return mcp_text(
                            {"result": "ERROR", "error": "Selector not found (timeout waiting attached)", "selector": selector, "current_url": page.url},
                            session_id,
                            page,
                        )

                    cnt = await page.locator(selector).count()
                    logger.debug(f"[DOWNLOAD] locator count={cnt}")
                    if cnt == 0:
                        return mcp_text(
                            {"result": "ERROR", "error": "Selector not found", "selector": selector, "current_url": page.url},
                            session_id,
                            page,
                        )

                    await page.locator(selector).first.scroll_into_view_if_needed(timeout=15000)
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.click(selector, force=True, timeout=15000)

                download = await download_info.value

                filename = download.suggested_filename or "download.bin"
                file_path = os.path.join(DOWNLOAD_PATH, filename)
                await download.save_as(file_path)

                size_bytes = os.path.getsize(file_path)
                sha256 = file_sha256(file_path)
                mime = guess_mime_type(filename)

                logger.info(f"[DOWNLOAD] saved={file_path} size={size_bytes} sha256={sha256}")

                payload = {
                    "result": "OK",
                    "filename": filename,
                    "path": file_path,
                    "mimeType": mime,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "chunk_recommended_bytes": DEFAULT_CHUNK_SIZE,
                    "current_url": page.url,
                }
                return mcp_text(payload, session_id, page)

            elif name == "download_pdf_wikipedia":
                try:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass

                    current = page.url
                    page_title = parse_query_param_from_url(current, "page")

                    if not page_title:
                        try:
                            page_title = await page.evaluate(
                                """
                                () => {
                                    const el = document.querySelector('input[name="page"]');
                                    return el ? (el.value || null) : null;
                                }
                                """
                            )
                        except Exception:
                            page_title = None

                    if not page_title:
                        return mcp_text(
                            {"result": "ERROR", "error": "Cannot determine wikipedia page title (param 'page' missing)", "current_url": current},
                            session_id,
                            page,
                        )

                    page_title_norm = str(page_title).replace(" ", "_")

                    source_url = wiki_pdf_rest_url(current, page_title_norm)
                    logger.info(f"[WIKI_PDF] REST url={source_url} (page={page_title_norm})")

                    resp = await page.request.get(
                        source_url,
                        timeout=120000,
                        headers={"accept": "application/pdf"},
                    )

                    ct = ""
                    try:
                        ct = resp.headers.get("content-type", "")
                    except Exception:
                        pass

                    body = await resp.body()
                    if not body:
                        return mcp_text(
                            {"result": "ERROR", "error": "Empty body", "source_url": source_url, "current_url": current, "content_type": ct},
                            session_id,
                            page,
                        )

                    is_pdf = False
                    if ct.lower().startswith("application/pdf"):
                        is_pdf = True
                    if body[:5] == b"%PDF-":
                        is_pdf = True

                    if not is_pdf:
                        snippet = body[:400].decode("utf-8", errors="replace")
                        return mcp_text(
                            {
                                "result": "ERROR",
                                "error": "Response is not a PDF",
                                "source_url": source_url,
                                "current_url": current,
                                "content_type": ct,
                                "size_bytes": len(body),
                                "body_snippet": snippet,
                                "page_title": page_title_norm,
                            },
                            session_id,
                            page,
                        )

                    filename = f"{page_title_norm}.pdf"
                    try:
                        cd = resp.headers.get("content-disposition", "")
                        fn = parse_filename_from_content_disposition(cd)
                        if fn:
                            filename = os.path.basename(fn)
                    except Exception:
                        pass

                    file_path = os.path.join(DOWNLOAD_PATH, filename)
                    with open(file_path, "wb") as f:
                        f.write(body)

                    size_bytes = os.path.getsize(file_path)
                    sha256 = file_sha256(file_path)

                    logger.info(f"[WIKI_PDF] saved={file_path} size={size_bytes} sha256={sha256}")

                    payload = {
                        "result": "OK",
                        "filename": filename,
                        "path": file_path,
                        "mimeType": "application/pdf",
                        "size_bytes": size_bytes,
                        "sha256": sha256,
                        "chunk_recommended_bytes": DEFAULT_CHUNK_SIZE,
                        "current_url": current,
                        "source_url": source_url,
                        "content_type": ct,
                        "page_title": page_title_norm,
                    }
                    return mcp_text(payload, session_id, page)

                except Exception as e:
                    logger.error(f"[ERROR] download_pdf_wikipedia: {str(e)}")
                    return mcp_text({"result": "ERROR", "error": str(e), "current_url": page.url}, session_id, page)

            elif name == "read_file_chunk":
                path = arguments["path"]
                offset = int(arguments["offset"])
                length = int(arguments["length"])

                abs_path = os.path.abspath(path)
                abs_dl = os.path.abspath(DOWNLOAD_PATH)
                logger.debug(f"[READ_CHUNK] path={abs_path} offset={offset} length={length}")

                if not abs_path.startswith(abs_dl + os.sep):
                    return mcp_text({"result": "ERROR", "error": "Forbidden path (must be inside DOWNLOAD_PATH)", "path": abs_path}, session_id, page)

                if not os.path.exists(abs_path):
                    return mcp_text({"result": "ERROR", "error": "File not found", "path": abs_path}, session_id, page)

                file_size = os.path.getsize(abs_path)
                if offset < 0 or length <= 0 or offset > file_size:
                    return mcp_text(
                        {"result": "ERROR", "error": "Invalid offset/length", "path": abs_path, "file_size": file_size, "offset": offset, "length": length},
                        session_id,
                        page,
                    )

                with open(abs_path, "rb") as f:
                    f.seek(offset)
                    data = f.read(length)

                payload = {
                    "result": "OK",
                    "path": abs_path,
                    "offset": offset,
                    "length": len(data),
                    "file_size": file_size,
                    "data_base64": base64.b64encode(data).decode("utf-8"),
                    "eof": (offset + len(data)) >= file_size,
                }
                return mcp_text(payload, session_id, page)

            elif name == "screenshot":
                img = await page.screenshot(type="png", full_page=False)
                return [types.ImageContent(type="image", data=base64.b64encode(img).decode(), mimeType="image/png")]

            else:
                return mcp_text({"result": "ERROR", "error": f"Unknown tool: {name}"}, session_id, page)

        except Exception as e:
            if attempt == 1 and is_target_closed_error(e):
                logger.warning(f"[RETRY] Target closed detected for session={session_id}. Recreating and retrying once. err={e}")
                await close_and_forget_session(session_id)
                continue

            logger.error(f"[ERROR] Technique: {str(e)}")
            return mcp_text({"result": "ERROR", "error": str(e)}, session_id, page)

    return mcp_text({"result": "ERROR", "error": "Unexpected retry fallthrough"}, session_id, page)


# --- ROUTAGE INFRA ---
async def sse_endpoint(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(sessions),
        "download_path": DOWNLOAD_PATH,
        "pod_url": POD_URL,
        "secret_name_prefix": SECRET_NAME_PREFIX,
    }


app.add_route("/sse", sse_endpoint, methods=["GET"])
app.mount("/messages/", app=sse_transport.handle_post_message)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        access_log=True,
        log_level=UVICORN_LOG_LEVEL,
    )
