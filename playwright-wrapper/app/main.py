import logging
from fastapi import FastAPI, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from playwright.async_api import async_playwright
from .settings import settings

# Logs
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("mcp-browserless")

app = FastAPI(title="n8n MCP Browserless Wrapper")
mcp_server = Server("playwright-tools")

# State global pour la session browser
state = {"browser": None, "context": None}

async def get_page():
    if not state["browser"]:
        pw = await async_playwright().start()
        # On se connecte à Browserless
        state["browser"] = await pw.chromium.connect_over_cdp(settings.browserless_url)
        state["context"] = await state["browser"].new_context()
    
    # On réutilise ou on crée une nouvelle page
    if not state["context"].pages:
        return await state["context"].new_page()
    return state["context"].pages[0]

# --- DÉFINITION DES OUTILS ---

@mcp_server.list_tools()
async def list_tools():
    return [
        {
            "name": "navigate",
            "description": "Aller sur une URL",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"]
            }
        },
        {
            "name": "click",
            "description": "Cliquer sur un élément (sélecteur CSS)",
            "inputSchema": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"]
            }
        }
    ]

@mcp_server.call_tool()
async def call_tool(name, arguments):
    page = await get_page()
    if name == "navigate":
        await page.goto(arguments["url"])
        return [{"type": "text", "text": f"OK: Navigué sur {arguments['url']}"}]
    elif name == "click":
        await page.click(arguments["selector"])
        return [{"type": "text", "text": f"OK: Cliqué sur {arguments['selector']}"}]

# --- TRANSPORT SSE ---
sse = SseServerTransport("/messages")

@app.get("/sse")
async def sse_endpoint(request: Request):
    async with sse.connect_scope(request.scope, request.receive, request._send):
        await mcp_server.run(
            sse.read_socket,
            sse.write_socket,
            mcp_server.create_initialization_options()
        )

@app.post("/messages")
async def messages_endpoint(request: Request):
    await sse.handle_post_request(request.scope, request.receive, request._send)
