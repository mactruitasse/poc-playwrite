const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { CallToolRequestSchema, ListToolsRequestSchema } = require("@modelcontextprotocol/sdk/types.js");
const { chromium } = require("playwright");
const express = require("express");

const app = express();
app.use(express.json({ limit: "5mb" })); // [cite: 3]

let browser, context;

// Initialisation asynchrone sécurisée de Playwright
(async () => {
  try {
    browser = await chromium.launch({ 
      headless: true, 
      args: [
        '--no-sandbox', 
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage', // Correction pour éviter les crashs en Docker [cite: 4]
        '--no-zygote'
      ] 
    });
    context = await browser.newContext({
      viewport: { width: 1280, height: 720 } // [cite: 4, 5]
    });
    console.log("🚀 Navigateur partagé prêt (Mode HTTP Streamable).");
  } catch (e) {
    console.error("❌ Erreur fatale Playwright:", e);
    process.exit(1);
  }
})();

// Création du serveur MCP [cite: 6]
const mcpServer = new Server(
  { name: "playwright-worker", version: "1.1.0" },
  { capabilities: { tools: {} } }
);

// Handler pour lister les outils [cite: 7]
mcpServer.setRequestHandler(ListToolsRequestSchema, async () => ({
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

// Handler pour l'exécution des outils [cite: 8, 9]
mcpServer.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "browser_navigate") return { content: [{ type: "text", text: "Outil inconnu" }], isError: true };
  const { url } = request.params.arguments || {};
  
  if (!context) return { content: [{ type: "text", text: "Navigateur non initialisé" }], isError: true };

  const page = await context.newPage();
  try {
    console.log(`[ACTION] Navigation : ${url}`);
    await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
    const title = await page.title();
    return { content: [{ type: "text", text: `Succès : ${title}` }] };
  } catch (e) {
    return { content: [{ type: "text", text: `Erreur : ${e.message}` }], isError: true };
  } finally {
    await page.close();
  }
});

// --- Nouveaux Endpoints pour la stabilité ---

// Endpoint de santé (évite les 404 pour Kubernetes)
app.get("/health", (req, res) => res.status(200).send("OK"));

// Endpoint MCP unifié (reçoit les POST de n8n)
app.post("/mcp", async (req, res) => {
  try {
    const response = await mcpServer.handleRequest(req.body);
    res.json(response);
  } catch (e) {
    console.error("❌ Erreur MCP:", e);
    res.status(500).json({ error: e.message });
  }
});

app.listen(8933, "0.0.0.0", () => console.log(`📡 MCP Worker prêt sur port 8933`)); // [cite: 14]
