import logging
from fastapi import FastAPI, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from playwright.async_api import async_playwright
from .settings import settings

# --- CONFIGURATION LOGS ---
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("mcp-browserless")

app = FastAPI(title="n8n MCP Browserless Wrapper")
mcp_server = Server("playwright-tools")

# --- ÉTAT GLOBAL (SESSION BROWSER) ---
state = {"browser": None, "context": None}

async def get_page():
    if not state["browser"]:
        logger.info(f"Connexion à Browserless sur {settings.browserless_url}")
        pw = await async_playwright().start()
        state["browser"] = await pw.chromium.connect_over_cdp(settings.browserless_url)
        state["context"] = await state["browser"].new_context()
    
    if not state["context"].pages:
        return await state["context"].new_page()
    return state["context"].pages[0]

# --- ENDPOINT DE SANTÉ ---
@app.get("/health")
async def health():
    return {"status": "ok", "browser_connected": state["browser"] is not None}

# --- DÉFINITION DES OUTILS MCP ---
@mcp_server.list_tools()
async def list_tools():
    return [
        {
            "name": "navigate",
            "description": "Naviguer vers une URL spécifique",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"]
            }
        },
        {
            "name": "click",
            "description": "Cliquer sur un élément CSS",
            "inputSchema": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"]
            }
        },
        {
            "name": "extract_content",
            "description": "Extraire tout le texte de la page",
            "inputSchema": {"type": "object", "properties": {}}
        }
    ]

@mcp_server.call_tool()
async def call_tool(name, arguments):
    page = await get_page()
    try:
        if name == "navigate":
            await page.goto(arguments["url"], wait_until="domcontentloaded")
            return [{"type": "text", "text": f"Succès : Navigué sur {arguments['url']}"}]
        elif name == "click":
            await page.click(arguments["selector"])
            return [{"type": "text", "text": f"Succès : Cliqué sur {arguments['selector']}"}]
        elif name == "extract_content":
            content = await page.content()
            return [{"type": "text", "text": content}]
    except Exception as e:
        logger.error(f"Erreur outil {name}: {str(e)}")
        return [{"type": "text", "text": f"Erreur : {str(e)}"}]

# --- TRANSPORT MCP (SSE) ---
sse_transport = SseServerTransport("/messages")

@app.get("/sse")
async def sse_endpoint(request: Request):
    # CHANGEMENT ICI : On utilise connect_sse et on récupère les flux
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            mcp_server.create_initialization_options()
        )

@app.post("/messages")
async def messages_endpoint(request: Request):
    await sse_transport.handle_post_request(request.scope, request.receive, request._send)
