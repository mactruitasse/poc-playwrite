import sys
import os

# On force le chemin
sys.path.append('/tmp/packages')

import asyncio
from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types
from playwright.async_api import async_playwright
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn

# 1. Initialisation du serveur MCP
core_server = Server("n8n-playwright-worker")

# 2. Définition de l'outil
@core_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="navigate_to",
            description="Navigue vers une URL avec Playwright",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"}
                },
                "required": ["url"],
            },
        )
    ]

# 3. Logique Playwright
@core_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "navigate_to":
        url = arguments.get("url")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                title = await page.title()
                return [types.TextContent(type="text", text=f"Titre : {title}")]
            except Exception as e:
                return [types.TextContent(type="text", text=f"Erreur : {str(e)}")]
            finally:
                await browser.close()
    return [types.TextContent(type="text", text="Inconnu")]

# 4. Configuration Transport SSE (Le pont avec n8n)
sse = SseServerTransport("/mcp")

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await core_server.run(read_stream, write_stream, core_server.create_initialization_options())

async def handle_messages(request):
    await sse.handle_post_message(request.scope, request.receive, request._send)

# 5. Application Starlette (serveur web léger)
app = Starlette(
    routes=[
        Route("/mcp", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081) # Change 8080 en 8081
