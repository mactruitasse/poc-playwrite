import logging
import asyncio
import base64
import json
import os
import sys
import shutil
import hashlib
from contextlib import asynccontextmanager
from urllib.parse import urlparse, parse_qs, quote

# --- LOGGING PLUS VERBEUX (contrôlé par ENV) ---
# LOG_LEVEL: INFO|DEBUG (par défaut DEBUG)
# UVICORN_LOG_LEVEL: debug|info|warning...
# PW_VERBOSE=1 pour logs Playwright (requests/responses/console)
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()
UVICORN_LOG_LEVEL = os.getenv("UVICORN_LOG_LEVEL", "debug").lower()
PW_VERBOSE = os.getenv("PW_VERBOSE", "0") == "1"

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
    logger.error(f"[FATAL] Dependance manquante dans le Pod : {e}")
    raise

# --- ARCHITECTURE IMMUABLE (KIND/PVC/CDP) ---
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/app/downloads")
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# Exemple: ws://browserless.n8n-prod.svc.cluster.local:3000
BROWSERLESS_URL = os.getenv(
    "BROWSERLESS_URL",
    "ws://browserless.n8n-prod.svc.cluster.local:3000",
)

# Limites de chunk (base64 gonfle ~33%).
# 512KB brut -> ~682KB base64, généralement safe.
DEFAULT_CHUNK_SIZE = int(os.getenv("CHUNK_SIZE_BYTES", "524288"))

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
    if fn.endswith(".pdf"):
        return "application/pdf"
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith(".jpg") or fn.endswith(".jpeg"):
        return "image/jpeg"
    if fn.endswith(".txt"):
        return "text/plain"
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
        # parse_qs renvoie une liste
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
    # quote encode le title pour un path segment
    return f"{origin}/api/rest_v1/page/pdf/{quote(page_title, safe='')}"


# --- ANALYSE DOM BRUTE (RAW Scout Report) ---
async def extract_deep_dom(page):
    logger.info("[DOM] Execution du RAW Scout Report (Extraction exhaustive)...")
    script = """
    () => {
        const elements = document.querySelectorAll('*');
        return Array.from(elements).map((el, index) => {
            const rect = el.getBoundingClientRect();
            return {
                index: index,
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                class: el.className || null,
                text: (el.innerText || '').trim().substring(0, 100),
                href: el.href || null,
                ariaExpanded: el.getAttribute('aria-expanded'),
                ariaLabel: el.getAttribute('aria-label'),
                isVisible: rect.width > 0 && rect.height > 0,
                rect: { w: rect.width, h: rect.height, t: rect.top, l: rect.left }
            };
        }).filter(el => !['script', 'style', 'meta', 'link', 'noscript'].includes(el.tag));
    }
    """
    return await page.evaluate(script)


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
            description="Navigation + RAW Scout DOM",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}, **s_id},
                "required": ["url", "session_id"],
            },
        ),
        types.Tool(
            name="scout_dom",
            description="Analyse exhaustive des elements actuels",
            inputSchema={"type": "object", "properties": {**s_id}, "required": ["session_id"]},
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
            description="Saisie de texte",
            inputSchema={
                "type": "object",
                "properties": {"selector": {"type": "string"}, "value": {"type": "string"}, **s_id},
                "required": ["selector", "value", "session_id"],
            },
        ),
        types.Tool(
            name="download_file",
            description=(
                "Telechargement via expect_download (Support 'idx:N'). "
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
            description="Capture d'ecran HD",
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
        return [types.TextContent(type="text", text="Stockage PVC nettoye.")]

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
                dom = await extract_deep_dom(page)
                payload = {"url": page.url, "scout_report": dom}
                return [types.TextContent(type="text", text=safe_json_text(payload))]

            elif name == "scout_dom":
                dom = await extract_deep_dom(page)
                payload = {"elements": dom}
                return [types.TextContent(type="text", text=safe_json_text(payload))]

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
                        return [types.TextContent(type="text", text=safe_json_text({"error": "idx element not found", "selector": selector}))]

                else:
                    cnt = await page.locator(selector).count()
                    logger.debug(f"[CLICK] locator count={cnt}")
                    if cnt == 0:
                        return [types.TextContent(type="text", text=safe_json_text({"error": "Selector not found", "selector": selector}))]
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

                logger.debug(f"[CLICK] new_url={page.url}")
                new_dom = await extract_deep_dom(page)
                payload = {"action": "click", "result": "OK", "new_url": page.url, "scout_report": new_dom}
                return [types.TextContent(type="text", text=safe_json_text(payload))]

            elif name == "fill_input":
                selector = arguments["selector"]
                value = arguments["value"]
                logger.info(f"[FILL] selector={selector} len(value)={len(value)}")
                await page.focus(selector)
                await page.type(selector, value, delay=30)
                payload = {"action": "fill", "result": "OK"}
                return [types.TextContent(type="text", text=safe_json_text(payload))]

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
                    cnt = await page.locator(selector).count()
                    logger.debug(f"[DOWNLOAD] locator count={cnt}")
                    if cnt == 0:
                        payload = {"result": "ERROR", "error": "Selector not found", "selector": selector, "current_url": page.url}
                        return [types.TextContent(type="text", text=safe_json_text(payload))]

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
                return [types.TextContent(type="text", text=safe_json_text(payload))]

            elif name == "download_pdf_wikipedia":
                # FIX Wikipedia (robuste):
                # - construit /api/rest_v1/page/pdf/<title> à partir de ?page=...
                # - fallback: input[name=page] dans le DOM
                # - télécharge via page.request.get avec Accept: application/pdf
                # - valide %PDF- avant de sauver
                try:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass

                    current = page.url
                    page_title = parse_query_param_from_url(current, "page")

                    if not page_title:
                        # Fallback: tenter de lire dans le DOM (si param absent)
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
                        payload = {"result": "ERROR", "error": "Cannot determine wikipedia page title (param 'page' missing)", "current_url": current}
                        return [types.TextContent(type="text", text=safe_json_text(payload))]

                    # Certains titres peuvent contenir des espaces; MediaWiki accepte underscores ou espaces,
                    # mais on normalise en underscores pour être safe.
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
                    if not body or len(body) == 0:
                        payload = {"result": "ERROR", "error": "Empty body", "source_url": source_url, "current_url": current, "content_type": ct}
                        return [types.TextContent(type="text", text=safe_json_text(payload))]

                    # Validation PDF (content-type OU magic bytes)
                    is_pdf = False
                    if ct.lower().startswith("application/pdf"):
                        is_pdf = True
                    if body[:5] == b"%PDF-":
                        is_pdf = True

                    if not is_pdf:
                        snippet = body[:400].decode("utf-8", errors="replace")
                        payload = {
                            "result": "ERROR",
                            "error": "Response is not a PDF",
                            "source_url": source_url,
                            "current_url": current,
                            "content_type": ct,
                            "size_bytes": len(body),
                            "body_snippet": snippet,
                            "page_title": page_title_norm,
                        }
                        return [types.TextContent(type="text", text=safe_json_text(payload))]

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
                    return [types.TextContent(type="text", text=safe_json_text(payload))]

                except Exception as e:
                    logger.error(f"[ERROR] download_pdf_wikipedia: {str(e)}")
                    payload = {"result": "ERROR", "error": str(e), "current_url": page.url}
                    return [types.TextContent(type="text", text=safe_json_text(payload))]

            elif name == "read_file_chunk":
                path = arguments["path"]
                offset = int(arguments["offset"])
                length = int(arguments["length"])

                abs_path = os.path.abspath(path)
                abs_dl = os.path.abspath(DOWNLOAD_PATH)
                logger.debug(f"[READ_CHUNK] path={abs_path} offset={offset} length={length}")

                if not abs_path.startswith(abs_dl + os.sep):
                    payload = {"result": "ERROR", "error": "Forbidden path (must be inside DOWNLOAD_PATH)", "path": abs_path}
                    return [types.TextContent(type="text", text=safe_json_text(payload))]

                if not os.path.exists(abs_path):
                    payload = {"result": "ERROR", "error": "File not found", "path": abs_path}
                    return [types.TextContent(type="text", text=safe_json_text(payload))]

                file_size = os.path.getsize(abs_path)
                if offset < 0 or length <= 0 or offset > file_size:
                    payload = {"result": "ERROR", "error": "Invalid offset/length", "path": abs_path, "file_size": file_size, "offset": offset, "length": length}
                    return [types.TextContent(type="text", text=safe_json_text(payload))]

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
                return [types.TextContent(type="text", text=safe_json_text(payload))]

            elif name == "screenshot":
                img = await page.screenshot(type="png", full_page=False)
                return [types.ImageContent(type="image", data=base64.b64encode(img).decode(), mimeType="image/png")]

            else:
                return [types.TextContent(type="text", text=safe_json_text({"result": "ERROR", "error": f"Unknown tool: {name}"}))]

        except Exception as e:
            if attempt == 1 and is_target_closed_error(e):
                logger.warning(f"[RETRY] Target closed detected for session={session_id}. Recreating and retrying once. err={e}")
                await close_and_forget_session(session_id)
                continue

            logger.error(f"[ERROR] Technique: {str(e)}")
            return [types.TextContent(type="text", text=safe_json_text({"result": "ERROR", "error": str(e)}))]

    return [types.TextContent(type="text", text=safe_json_text({"result": "ERROR", "error": "Unexpected retry fallthrough"}))]


# --- ROUTAGE INFRA ---
async def sse_endpoint(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(sessions), "download_path": DOWNLOAD_PATH}


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
