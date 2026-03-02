import logging
import asyncio
import base64
import json
import os
import sys
import shutil
from contextlib import asynccontextmanager

# --- CONFIGURATION LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp-manager")

try:
    import uvicorn
    from fastapi import FastAPI, Request, Response
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    import mcp.types as types
    from playwright.async_api import async_playwright
except ImportError as e:
    logger.error(f"💥 Dépendance manquante : {e}")
    raise

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/app/downloads")
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

mcp_server = Server("playwright-tools")
sessions: dict[str, dict] = {}
pw_manager = None
BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "ws://browserless.n8n-prod.svc.cluster.local:3000")
sse_transport = SseServerTransport("/messages/")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pw_manager
    pw_manager = await async_playwright().start()
    yield
    for _s_id, data in list(sessions.items()):
        try: await data["browser"].close()
        except: pass
    await pw_manager.stop()

app = FastAPI(title="n8n-Persistent-Scout", lifespan=lifespan)

async def analyze_page_raw(page):
    script = """
    () => {
        const elements = document.querySelectorAll('button, input, a, select, textarea, [role="button"]');
        return Array.from(elements).map((el, index) => {
            const rect = el.getBoundingClientRect();
            return {
                index: index, tag: el.tagName.toLowerCase(), 
                text: (el.innerText || el.value || '').trim().substring(0, 50),
                isVisible: rect.width > 0 && rect.height > 0
            };
        });
    }
    """
    return await page.evaluate(script)

async def get_or_create_session(session_id: str):
    if session_id in sessions:
        data = sessions[session_id]
        if data["browser"].is_connected(): return data["page"]
    browser = await pw_manager.chromium.connect_over_cdp(BROWSERLESS_URL)
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await context.new_page()
    sessions[session_id] = {"context": context, "page": page, "browser": browser}
    return page

@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    s_id = {"session_id": {"type": "string"}}
    return [
        types.Tool(name="navigate", description="Navigue", inputSchema={"type":"object","properties":{"url":{"type":"string"},**s_id},"required":["url","session_id"]}),
        types.Tool(name="scout_dom", description="Analyse DOM", inputSchema={"type":"object","properties":{**s_id},"required":["session_id"]}),
        types.Tool(name="click_element", description="Clic", inputSchema={"type":"object","properties":{"selector":{"type":"string"},**s_id},"required":["selector","session_id"]}),
        types.Tool(name="fill_input", description="Saisie", inputSchema={"type":"object","properties":{"selector":{"type":"string"},"value":{"type":"string"},**s_id},"required":["selector","value","session_id"]}),
        types.Tool(name="download_file", description="Download Binaire Direct", inputSchema={"type":"object","properties":{"selector":{"type":"string"},**s_id},"required":["selector","session_id"]}),
        types.Tool(name="screenshot", description="Capture Binaire Direct", inputSchema={"type":"object","properties":{**s_id},"required":["session_id"]}),
        types.Tool(name="purge_downloads", description="Nettoie PVC", inputSchema={"type":"object","properties":{},"required":[]}),
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "purge_downloads":
        shutil.rmtree(DOWNLOAD_PATH); os.makedirs(DOWNLOAD_PATH)
        return [types.TextContent(type="text", text="Dossier vidé")]

    session_id = arguments.get("session_id")
    page = await get_or_create_session(session_id)
    
    try:
        if name == "navigate":
            await page.goto(arguments["url"], wait_until="networkidle")
            return [types.TextContent(type="text", text=page.url)]

        elif name == "scout_dom":
            elements = await analyze_page_raw(page)
            return [types.TextContent(type="text", text=json.dumps(elements))]

        elif name == "click_element":
            await page.click(arguments["selector"], force=True)
            return [types.TextContent(type="text", text="OK")]

        elif name == "fill_input":
            await page.type(arguments["selector"], arguments["value"], delay=50)
            return [types.TextContent(type="text", text="OK")]

        elif name == "download_file":
            async with page.expect_download() as download_info:
                await page.click(arguments["selector"], force=True)
            download = await download_info.value
            file_path = os.path.join(DOWNLOAD_PATH, download.suggested_filename)
            await download.save_as(file_path)
            
            with open(file_path, "rb") as f:
                data_b64 = base64.b64encode(f.read()).decode()
            
            # --- IMPORTANT: Retourne UNIQUEMENT le Blob pour forcer l'onglet binaire ---
            return [types.BlobContent(type="blob", blob=data_b64, mimeType="application/octet-stream")]

        elif name == "screenshot":
            img = await page.screenshot(type="png")
            return [types.BlobContent(type="blob", blob=base64.b64encode(img).decode(), mimeType="image/png")]

    except Exception as e:
        logger.error(f"Error: {e}")
        return [types.TextContent(type="text", text=f"Erreur: {str(e)}")]

# --- INFRA ---
async def sse_endpoint(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())

@app.get("/health")
async def health(): return {"status": "ok"}

app.mount("/", Starlette(routes=[
    Route("/sse", sse_endpoint, methods=["GET"]),
    Mount("/messages/", app=sse_transport.handle_post_message),
]))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
