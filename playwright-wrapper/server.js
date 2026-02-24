const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { SSEServerTransport } = require("@modelcontextprotocol/sdk/server/sse.js");
const { CallToolRequestSchema, ListToolsRequestSchema } = require("@modelcontextprotocol/sdk/types.js");
const { chromium } = require("playwright");
const express = require("express");
const crypto = require("crypto");

const app = express();
app.use(express.json({ limit: "5mb" }));

let browser, context;

// Initialisation asynchrone sécurisée
(async () => {
  try {
    browser = await chromium.launch({ 
      headless: true, 
      args: [
        '--no-sandbox', 
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage', // CORRECTION 1 : Évite les crashs de mémoire en Docker
        '--no-zygote'
      ] 
    });
    context = await browser.newContext({
      viewport: { width: 1280, height: 720 }
    });
    console.log("🚀 Navigateur partagé prêt.");
  } catch (e) {
    console.error("❌ Erreur fatale Playwright:", e);
    process.exit(1);
  }
})();

function createMcpServer() {
  const s = new Server(
    { name: "playwright-worker", version: "1.0.0" },
    { capabilities: { tools: {} } }
  );

  s.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [{
      name: "browser_navigate",
      description: "Naviguer vers une URL avec le contexte partagé",
      inputSchema: {
        type: "object",
        properties: { url: { type: "string" } },
        required: ["url"],
      },
    }],
  }));

  s.setRequestHandler(CallToolRequestSchema, async (request) => {
    if (request.params.name !== "browser_navigate") return { content: [{ type: "text", text: "Outil inconnu" }], isError: true };
    const { url } = request.params.arguments || {};
    
    // CORRECTION 2 : Vérification du contexte avant usage
    if (!context) return { content: [{ type: "text", text: "Navigateur non initialisé" }], isError: true };

    const page = await context.newPage();
    try {
      console.log(`[ACTION] Navigation vers : ${url}`);
      // On utilise networkidle pour être sûr que la page est chargée
      await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
      const title = await page.title();
      return { content: [{ type: "text", text: `Succès : ${title}` }] };
    } catch (e) {
      return { content: [{ type: "text", text: `Erreur : ${e.message}` }], isError: true };
    } finally {
      await page.close();
    }
  });
  return s;
}

const sessions = new Map();

app.get("/sse", async (req, res) => {
  const sessionId = req.query.sessionId || req.query.session || crypto.randomBytes(8).toString("hex");
  
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
  });

  res.write(": welcome\n\n");

  const mcpServer = createMcpServer();
  // Le transport a besoin de l'URL relative pour que le client sache où POSTer
  const transport = new SSEServerTransport("/messages", res); 

  sessions.set(sessionId, { transport, server: mcpServer });

  try {
    await mcpServer.connect(transport);
    console.log(`==> [SSE] Session connectée : ${sessionId}`);
  } catch (e) {
    console.error(`❌ Erreur session ${sessionId}:`, e);
    res.end();
  }

  req.on("close", () => {
    console.log(`==> [SSE] Client déconnecté : ${sessionId}`);
    setTimeout(() => sessions.delete(sessionId), 5000); // Délai de grâce
  });
});

app.post("/messages", async (req, res) => {
  // On récupère le sessionId soit dans l'URL, soit on cherche la session correspondante
  const sessionId = req.query.sessionId || req.query.session;
  
  // Si le SDK n8n ne passe pas le sessionId dans l'URL du POST, 
  // on peut essayer de retrouver la session via le transport (plus complexe)
  // Mais ici, n8n suit normalement l'URL fournie par SSEServerTransport.
  
  const sessionData = sessions.get(sessionId);
  if (!sessionData) {
    return res.status(400).send("Session introuvable");
  }

  try {
    await sessionData.transport.handlePostMessage(req, res);
  } catch (e) {
    console.error("❌ Erreur message:", e);
    res.status(500).end();
  }
});

app.listen(8933, "0.0.0.0", () => console.log(`📡 MCP Worker sur port 8933`));
