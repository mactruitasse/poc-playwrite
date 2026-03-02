import logging
import asyncio
import base64
import json
import os
import sys
import shutil
from contextlib import asynccontextmanager

# --- CONFIGURATION LOGGING (Ultra-Verbeuse) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp-manager")

try:
    import uvicorn
    from fastapi import FastAPI, Request
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    import mcp.types as types
    from playwright.async_api import async_playwright
except ImportError as e:
    logger.error(f"💥 Dépendance manquante dans le Pod : {e}")
    raise

# --- ARCHITECTURE IMMUABLE : CONFIGURATION STORAGE ---
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/app/downloads")
os.makedirs(DOWNLOAD_PATH, exist_ok=True)
BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "ws://browserless.n8n-prod.svc.cluster.local:3000")

mcp_server = Server("playwright-tools")
sessions: dict[str, dict] = {}
pw_manager = None
sse_transport = SseServerTransport("/messages/")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pw_manager
    logger.info("🚀 [SYSTEM] Start Playwright Engine and CDP Connection...")
    pw_manager = await async_playwright().start()
    yield
    logger.info("🛑 [SYSTEM] Cleaning sessions and stopping engine...")
    for _s_id, data in list(sessions.items()):
        try: await data["browser"].close()
        except: pass
    await pw_manager.stop()

app = FastAPI(title="n8n-Persistent-Scout", lifespan=lifespan)

# --- FONCTION D'ANALYSE PROFONDE DU DOM ---
async def extract_deep_dom(page):
    logger.info("🔍 [DOM] Exécution du script d'analyse profonde (Scout Report)...")
    script = """
    () => {
        const elements = document.querySelectorAll('button, input, a, select, textarea, [role="button"], h1, h2, h3, p, span, div');
        return Array.from(elements).map((el, index) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            // On ne garde que les éléments visibles avec du contenu
            if (rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && (el.innerText.trim().length > 0 || el.tagName === 'INPUT')) {
                return {
                    index: index,
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    class: el.className || null,
                    text: (el.innerText || el.value || '').trim().substring(0, 100),
                    href: el.href || null,
                    isVisible: true
                };
            }
            return null;
        }).filter(x => x !== null);
    }
    """
    return await page.evaluate(script)

async def get_or_create_session(session_id: str):
    if session_id in sessions:
        data = sessions[session_id]
        if data["browser"].is_connected(): return data["page"]
    
    logger.info(f"🆕 [SESSION] Creation: {session_id}")
    browser = await pw_manager.chromium.connect_over_cdp(BROWSERLESS_URL)
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    page = await context.new_page()
    sessions[session_id] = {"context": context, "page": page, "browser": browser}
    return page

@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    s_id = {"session_id": {"type": "string"}}
    return [
        types.Tool(name="navigate", description="Navigue et analyse tout le site (DOM)", inputSchema={"type":"object","properties":{"url":{"type":"string"},**s_id},"required":["url","session_id"]}),
        types.Tool(name="scout_dom", description="Analyse profonde des sélecteurs actuels", inputSchema={"type":"object","properties":{**s_id},"required":["session_id"]}),
        types.Tool(name="click_element", description="Clic forcé via sélecteur", inputSchema={"type":"object","properties":{"selector":{"type":"string"},**s_id},"required":["selector","session_id"]}),
        types.Tool(name="fill_input", description="Saisie de texte", inputSchema={"type":"object","properties":{"selector":{"type":"string"},"value":{"type":"string"},**s_id},"required":["selector","value","session_id"]}),
        types.Tool(name="download_file", description="Téléchargement direct (Output Binaire)", inputSchema={"type":"object","properties":{"selector":{"type":"string"},**s_id},"required":["selector","session_id"]}),
        types.Tool(name="screenshot", description="Capture d'écran HD (Output Binaire)", inputSchema={"type":"object","properties":{**s_id},"required":["session_id"]}),
        types.Tool(name="purge_downloads", description="Vide le stockage persistant PVC", inputSchema={"type":"object","properties":{},"required":[]}),
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "purge_downloads":
        logger.info(f"🧹 [PVC] Purge du dossier {DOWNLOAD_PATH}")
        shutil.rmtree(DOWNLOAD_PATH)
        os.makedirs(DOWNLOAD_PATH)
        return [types.TextContent(type="text", text="🧹 Stockage PVC nettoyé.")]

    session_id = arguments.get("session_id")
    page = await get_or_create_session(session_id)
    logger.info(f"🛠️  EXECUTE: {name} | SESSION: {session_id}")
    
    try:
        if name == "navigate":
            await page.goto(arguments["url"], wait_until="networkidle", timeout=60000)
            dom_report = await extract_deep_dom(page)
            # On renvoie l'URL et le rapport DOM complet
            return [types.TextContent(
                type="text", 
                text=json.dumps({"url": page.url, "scout_report": dom_report}, indent=2)
            )]

        elif name == "scout_dom":
            dom_report = await extract_deep_dom(page)
            return [types.TextContent(type="text", text=json.dumps({"elements": dom_report}, indent=2))]

        elif name == "click_element":
            await page.click(arguments["selector"], force=True, timeout=15000)
            await page.wait_for_load_state("networkidle")
            return [types.TextContent(type="text", text=json.dumps({"action": "click", "result": "OK"}))]

        elif name == "fill_input":
            await page.focus(arguments["selector"])
            await page.type(arguments["selector"], arguments["value"], delay=50)
            return [types.TextContent(type="text", text=json.dumps({"action": "fill", "result": "OK"}))]

        elif name == "download_file":
            async with page.expect_download() as download_info:
                await page.click(arguments["selector"], force=True)
            download = await download_info.value
            file_path = os.path.join(DOWNLOAD_PATH, download.suggested_filename)
            await download.save_as(file_path)
            with open(file_path, "rb") as f:
                content = f.read()
            return [types.BlobContent(
                type="blob",
                blob=base64.b64encode(content).decode(),
                mimeType="application/octet-stream"
            )]

        elif name == "screenshot":
            img = await page.screenshot(type="png", full_page=False)
            return [types.BlobContent(
                type="blob",
                blob=base64.b64encode(img).decode(),
                mimeType="image/png"
            )]

    except Exception as e:
        logger.error(f"❌ [ERROR] in {name}: {str(e)}")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

# --- SSE INFRA ---
async def sse_endpoint(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())

@app.get("/health")
async def health(): return {"status": "ok", "sessions": len(sessions)}

app.mount("/", Starlette(routes=[
    Route("/sse", sse_endpoint, methods=["GET"]),
    Mount("/messages/", app=sse_transport.handle_post_message),
]))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, access_log=True)
