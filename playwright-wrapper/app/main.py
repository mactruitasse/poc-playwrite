import logging
import asyncio
import base64
import json
import os
import sys
import shutil
import hashlib
from contextlib import asynccontextmanager

# --- CONFIGURATION LOGGING (Direct et Transparent) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp-manager")

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
            # Important: fermer context puis browser
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


async def get_or_create_session(session_id: str):
    # Session existante encore utilisable
    if session_id in sessions:
        data = sessions[session_id]
        try:
            if data["browser"].is_connected() and not data["page"].is_closed():
                return data["page"]
        except Exception:
            pass

    logger.info(f"[CDP] Creation d'une session DESKTOP HD (1920x1080) : {session_id}")

    # Connexion CDP vers browserless/chrome
    browser = await pw_manager.chromium.connect_over_cdp(BROWSERLESS_URL)

    # Important pour download interception : accept_downloads=True
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
            description="Telechargement robuste (retourne meta + path + taille + sha256). Utiliser read_file_chunk pour récupérer le binaire.",
            inputSchema={
                "type": "object",
                "properties": {"selector": {"type": "string"}, **s_id},
                "required": ["selector", "session_id"],
            },
        ),
        types.Tool(
            name="read_file_chunk",
            description="Lire un fichier local (path) en base64 par morceaux (chunk).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "length": {"type": "integer"},
                    **s_id,
                },
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
    page = await get_or_create_session(session_id)
    logger.info(f"[EXECUTE] {name} | SESSION: {session_id}")

    try:
        if name == "navigate":
            await page.goto(arguments["url"], wait_until="networkidle", timeout=60000)
            dom = await extract_deep_dom(page)
            payload = {"url": page.url, "scout_report": dom}
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        elif name == "scout_dom":
            dom = await extract_deep_dom(page)
            payload = {"elements": dom}
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        elif name == "click_element":
            selector = arguments["selector"]
            wait_for = arguments.get("wait_for_selector")

            if selector.startswith("idx:"):
                index = int(selector.split(":")[1])
                logger.info(f"[DOM] Clic force via JS sur l'index : {index}")
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
                    return [types.TextContent(type="text", text=json.dumps({"error": "idx element not found", "selector": selector}))]

            else:
                if await page.locator(selector).count() == 0:
                    return [types.TextContent(type="text", text=json.dumps({"error": "Selector not found", "selector": selector}))]

                await page.locator(selector).first.scroll_into_view_if_needed(timeout=15000)
                await page.click(selector, force=True, timeout=15000)

            if wait_for:
                logger.info(f"[WAIT] Attente de : {wait_for}")
                await page.wait_for_selector(wait_for, state="attached", timeout=15000)
            else:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.6)

            new_dom = await extract_deep_dom(page)
            payload = {"action": "click", "result": "OK", "new_url": page.url, "scout_report": new_dom}
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        elif name == "fill_input":
            await page.focus(arguments["selector"])
            await page.type(arguments["selector"], arguments["value"], delay=30)
            return [types.TextContent(type="text", text=json.dumps({"action": "fill", "result": "OK"}))]

        elif name == "download_file":
            selector = arguments["selector"]
            logger.info(f"[DOWNLOAD] Strategie robuste sur : {selector}")

            try:
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
                    if await page.locator(selector).count() == 0:
                        return [types.TextContent(type="text", text=json.dumps({"result": "ERROR", "error": "Selector not found", "selector": selector, "current_url": page.url}, indent=2))]

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

                logger.info(f"[SUCCESS] Fichier recupere : {file_path} | size={size_bytes} bytes")

                # IMPORTANT: ne pas renvoyer le base64 complet (n8n/MCP limite de payload)
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
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

            except Exception as e:
                logger.error(f"[ERROR] Echec interception download : {str(e)}")
                payload = {"result": "ERROR", "error": str(e), "selector": selector, "current_url": page.url}
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        elif name == "read_file_chunk":
            path = arguments["path"]
            offset = int(arguments["offset"])
            length = int(arguments["length"])

            # Sécurité: empêcher lecture hors du dossier download
            abs_path = os.path.abspath(path)
            abs_dl = os.path.abspath(DOWNLOAD_PATH)
            if not abs_path.startswith(abs_dl + os.sep) and abs_path != abs_dl:
                payload = {"result": "ERROR", "error": "Forbidden path (must be inside DOWNLOAD_PATH)", "path": abs_path}
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

            if not os.path.exists(abs_path):
                payload = {"result": "ERROR", "error": "File not found", "path": abs_path}
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

            file_size = os.path.getsize(abs_path)
            if offset < 0 or length <= 0 or offset > file_size:
                payload = {"result": "ERROR", "error": "Invalid offset/length", "path": abs_path, "file_size": file_size, "offset": offset, "length": length}
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

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
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        elif name == "screenshot":
            img = await page.screenshot(type="png", full_page=False)
            return [types.ImageContent(type="image", data=base64.b64encode(img).decode(), mimeType="image/png")]

        else:
            return [types.TextContent(type="text", text=json.dumps({"result": "ERROR", "error": f"Unknown tool: {name}"}))]

    except Exception as e:
        logger.error(f"[ERROR] Technique: {str(e)}")
        return [types.TextContent(type="text", text=json.dumps({"result": "ERROR", "error": str(e)}))]


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
    uvicorn.run(app, host="0.0.0.0", port=8080, access_log=True)
