import logging
import asyncio
import base64
import json
import os
import sys
import shutil
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

    # Optionnel : éviter certains pièges sur pages lourdes
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
            description="Telechargement (Support 'idx:N') robuste",
            inputSchema={
                "type": "object",
                "properties": {"selector": {"type": "string"}, **s_id},
                "required": ["selector", "session_id"],
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
            return [types.TextContent(type="text", text=json.dumps({"url": page.url, "scout_report": dom}, indent=2))]

        elif name == "scout_dom":
            dom = await extract_deep_dom(page)
            return [types.TextContent(type="text", text=json.dumps({"elements": dom}, indent=2))]

        elif name == "click_element":
            selector = arguments["selector"]
            wait_for = arguments.get("wait_for_selector")

            if selector.startswith("idx:"):
                index = int(selector.split(":")[1])
                logger.info(f"[DOM] Clic force via JS sur l'index : {index}")
                await page.evaluate(
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
            else:
                if await page.locator(selector).count() == 0:
                    return [types.TextContent(type="text", text=json.dumps({"error": "Selector not found", "selector": selector}))]
                # scroll + click
                await page.locator(selector).first.scroll_into_view_if_needed(timeout=15000)
                await page.click(selector, force=True, timeout=15000)

            if wait_for:
                logger.info(f"[WAIT] Attente de : {wait_for}")
                await page.wait_for_selector(wait_for, state="attached", timeout=15000)
            else:
                # petite stabilisation
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.6)

            new_dom = await extract_deep_dom(page)
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {"action": "click", "result": "OK", "new_url": page.url, "scout_report": new_dom},
                        indent=2,
                    ),
                )
            ]

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
                        return [types.TextContent(type="text", text=json.dumps({"error": "Selector not found", "selector": selector}))]

                    await page.locator(selector).first.scroll_into_view_if_needed(timeout=15000)

                    # Arm + click (sans create_task)
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.click(selector, force=True, timeout=15000)

                download = await download_info.value

                # Sauvegarde sur PVC
                filename = download.suggested_filename or "download.bin"
                file_path = os.path.join(DOWNLOAD_PATH, filename)
                await download.save_as(file_path)

                logger.info(f"[SUCCESS] Fichier recupere : {file_path}")

                with open(file_path, "rb") as f:
                    content = f.read()

                payload = {
                    "result": "OK",
                    "filename": filename,
                    "path": file_path,
                    "mimeType": "application/octet-stream",
                    "data_base64": base64.b64encode(content).decode("utf-8"),
                    "current_url": page.url,
                }
                return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

            except Exception as e:
                logger.error(f"[ERROR] Echec interception download : {str(e)}")
                return [
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "result": "ERROR",
                                "error": str(e),
                                "selector": selector,
                                "current_url": page.url,
                            },
                            indent=2,
                        ),
                    )
                ]

        elif name == "screenshot":
            img = await page.screenshot(type="png", full_page=False)
            return [types.ImageContent(type="image", data=base64.b64encode(img).decode(), mimeType="image/png")]

    except Exception as e:
        logger.error(f"[ERROR] Technique: {str(e)}")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


# --- ROUTAGE INFRA ---
async def sse_endpoint(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(sessions)}


app.add_route("/sse", sse_endpoint, methods=["GET"])
app.mount("/messages/", app=sse_transport.handle_post_message)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, access_log=True)
