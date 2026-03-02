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

BROWSERLESS_URL = os.getenv(
    "BROWSERLESS_URL",
    "ws://browserless.n8n-prod.svc.cluster.local:3000",
)

# Limites de chunk pour eviter de saturer le transport SSE/MCP
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
            if "context" in data: await data["context"].close()
            if "browser" in data: await data["browser"].close()
        except Exception as e:
            logger.warning(f"[WARN] Erreur fermeture session {_s_id}: {e}")
    await pw_manager.stop()

app = FastAPI(title="n8n-Persistent-Scout", lifespan=lifespan)

# --- UTILS TECHNIQUES ---
async def extract_deep_dom(page):
    logger.info("[DOM] Execution du RAW Scout Report...")
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
                isVisible: rect.width > 0 && rect.height > 0,
                rect: { w: rect.width, h: rect.height, t: rect.top, l: rect.left }
            };
        }).filter(el => !['script', 'style', 'meta', 'link', 'noscript'].includes(el.tag));
    }
    """
    return await page.evaluate(script)

def guess_mime_type(filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".pdf"): return "application/pdf"
    if fn.endswith(".png"): return "image/png"
    if fn.endswith(".jpg") or fn.endswith(".jpeg"): return "image/jpeg"
    return "application/octet-stream"

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

async def get_or_create_session(session_id: str):
    if session_id in sessions:
        data = sessions[session_id]
        try:
            if data["browser"].is_connected() and not data["page"].is_closed():
                return data["page"]
        except: pass

    logger.info(f"[CDP] Nouvelle session HD : {session_id}")
    browser = await pw_manager.chromium.connect_over_cdp(BROWSERLESS_URL)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        accept_downloads=True
    )
    page = await context.new_page()
    page.set_default_timeout(30000)
    sessions[session_id] = {"context": context, "page": page, "browser": browser}
    return page

# --- DEFINITION DES OUTILS ---
@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    s_id = {"session_id": {"type": "string", "description": "ID de session persistante"}}
    return [
        types.Tool(name="navigate", description="Navigation + DOM", inputSchema={"type":"object","properties":{"url":{"type":"string"},**s_id},"required":["url","session_id"]}),
        types.Tool(name="scout_dom", description="Analyse DOM", inputSchema={"type":"object","properties":{**s_id},"required":["session_id"]}),
        types.Tool(name="click_element", description="Clic (idx:N supporte)", inputSchema={"type":"object","properties":{"selector":{"type":"string"},"wait_for_selector":{"type":"string"},**s_id},"required":["selector","session_id"]}),
        types.Tool(name="download_file", description="Telechargement robuste (retourne meta). Utiliser read_file_chunk pour le binaire.", inputSchema={"type":"object","properties":{"selector":{"type":"string"},**s_id},"required":["selector","session_id"]}),
        types.Tool(name="read_file_chunk", description="Lire un fichier par morceaux", inputSchema={"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer"},"length":{"type":"integer"},**s_id},"required":["path","offset","length","session_id"]}),
        types.Tool(name="screenshot", description="Capture PNG HD", inputSchema={"type":"object","properties":{**s_id},"required":["session_id"]}),
        types.Tool(name="purge_downloads", description="Vider /app/downloads", inputSchema={"type":"object","properties":{},"required":[]}),
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "purge_downloads":
        shutil.rmtree(DOWNLOAD_PATH, ignore_errors=True)
        os.makedirs(DOWNLOAD_PATH, exist_ok=True)
        return [types.TextContent(type="text", text="Stockage PVC nettoye.")]

    session_id = arguments.get("session_id")
    page = await get_or_create_session(session_id)
    
    try:
        if name == "navigate":
            await page.goto(arguments["url"], wait_until="networkidle", timeout=60000)
            dom = await extract_deep_dom(page)
            return [types.TextContent(type="text", text=json.dumps({"url": page.url, "scout_report": dom}))]

        elif name == "click_element":
            selector = arguments["selector"]
            if selector.startswith("idx:"):
                index = int(selector.split(":")[1])
                await page.evaluate("(idx) => { const el = document.querySelectorAll('*')[idx]; el.scrollIntoView(); el.click(); }", index)
            else:
                await page.click(selector, force=True)
            return [types.TextContent(type="text", text=json.dumps({"result": "OK"}))]

        elif name == "download_file":
            selector = arguments["selector"]
            logger.info(f"[DOWNLOAD] Lancement interception sur : {selector}")

            try:
                async with page.expect_download(timeout=60000) as download_info:
                    if selector.startswith("idx:"):
                        index = int(selector.split(":")[1])
                        # Simulation clic robuste
                        await page.evaluate("(idx) => { const el = document.querySelectorAll('*')[idx]; el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); el.click(); }", index)
                    else:
                        await page.click(selector, force=True)
                
                download = await download_info.value
                # FORCE l'attente du flux binaire pour eviter le fichier a 0 octet
                temp_path = await download.path() 
                
                filename = download.suggested_filename or "download.bin"
                file_path = os.path.join(DOWNLOAD_PATH, filename)
                shutil.copy(temp_path, file_path)

                payload = {
                    "result": "OK",
                    "filename": filename,
                    "path": file_path,
                    "size_bytes": os.path.getsize(file_path),
                    "sha256": file_sha256(file_path),
                    "mimeType": guess_mime_type(filename),
                    "chunk_recommended_bytes": DEFAULT_CHUNK_SIZE
                }
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
            except Exception as e:
                return [types.TextContent(type="text", text=json.dumps({"result": "ERROR", "error": str(e)}))]

        elif name == "read_file_chunk":
            path, offset, length = arguments["path"], int(arguments["offset"]), int(arguments["length"])
            # Securite Path Traversal
            if not os.path.abspath(path).startswith(os.path.abspath(DOWNLOAD_PATH)):
                return [types.TextContent(type="text", text=json.dumps({"error": "Forbidden path"}))]
            
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(length)
            
            return [types.TextContent(type="text", text=json.dumps({
                "data_base64": base64.b64encode(data).decode("utf-8"),
                "eof": (offset + len(data)) >= os.path.getsize(path)
            }))]

        elif name == "screenshot":
            img = await page.screenshot(type="png")
            return [types.ImageContent(type="image", data=base64.b64encode(img).decode(), mimeType="image/png")]

    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

# --- ROUTAGE SSE ---
async def sse_endpoint(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())

app.add_route("/sse", sse_endpoint, methods=["GET"])
app.mount("/messages/", app=sse_transport.handle_post_message)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
