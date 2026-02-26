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
    """Récupère ou initialise la session Browserless."""
    if not state["browser"]:
        logger.info(f"Connexion à Browserless sur {settings.browserless_url}")
        pw = await async_playwright().start()
        # Connexion via le protocole CDP (Chrome DevTools Protocol)
        state["browser"] = await pw.chromium.connect_over_cdp(settings.browserless_url)
        state["context"] = await state["browser"].new_context()
    
    # On s'assure d'avoir au moins une page ouverte
    if not state["context"].pages:
        return await state["context"].new_page()
    return state["context"].pages[0]

# --- ENDPOINT DE SANTÉ (FIX KUBERNETES) ---
@app.get("/health")
async def health():
    """Répond aux probes Kubernetes pour éviter le crashloop."""
    return {"status": "ok", "browser_connected": state["browser"] is not None}

# --- DÉFINITION DES OUTILS MCP ---

@mcp_server.list_tools()
async def list_tools():
    """Liste les outils disponibles pour l'Agent IA n8n."""
    return [
        {
            "name": "navigate",
            "description": "Naviguer vers une URL spécifique",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "L'URL complète (ex: https://n8n.io)"}
                },
                "required": ["url"]
            }
        },
        {
            "name": "click",
            "description": "Cliquer sur un élément de la page via un sélecteur CSS",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Sélecteur CSS (ex: button.submit)"}
                },
                "required": ["selector"]
            }
        },
        {
            "name": "extract_content",
            "description": "Extraire tout le texte de la page actuelle",
            "inputSchema": {"type": "object", "properties": {}}
        }
    ]

@mcp_server.call_tool()
async def call_tool(name, arguments):
    """Exécute l'action demandée par l'IA sur Browserless."""
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
        logger.error(f"Erreur lors de l'exécution de l'outil {name}: {str(e)}")
        return [{"type": "text", "text": f"Erreur : {str(e)}"}]

# --- TRANSPORT MCP (SSE) ---

# On définit le transport SSE pour les messages MCP
sse_transport = SseServerTransport("/messages")

@app.get("/sse")
async def sse_endpoint(request: Request):
    """Point d'entrée pour établir la connexion MCP avec n8n."""
    async with sse_transport.connect_scope(request.scope, request.receive, request._send):
        await mcp_server.run(
            sse_transport.read_socket,
            sse_transport.write_socket,
            mcp_server.create_initialization_options()
        )

@app.post("/messages")
async def messages_endpoint(request: Request):
    """Endpoint de réception des commandes JSON-RPC envoyées par n8n."""
    await sse_transport.handle_post_request(request.scope, request.receive, request._send)
