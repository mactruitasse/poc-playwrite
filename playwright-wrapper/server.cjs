const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { CallToolRequestSchema, ListToolsRequestSchema } = require("@modelcontextprotocol/sdk/types.js");
const { chromium } = require("playwright");
const express = require("express");

const app = express();
app.use(express.json({ limit: "5mb" }));

let browser, context;

(async () => {
  try {
    browser = await chromium.launch({ 
      headless: true, 
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--no-zygote'] 
    });
    context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
    console.log("🚀 Navigateur prêt (Mode HTTP).");
  } catch (e) {
    console.error("❌ Erreur Playwright:", e);
    process.exit(1);
  }
})();

const mcpServer = new Server(
  { name: "playwright-worker", version: "1.1.0" },
  { capabilities: { tools: {} } }
);

mcpServer.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: "browser_navigate",
    description: "Naviguer vers une URL",
    inputSchema: {
      type: "object",
      properties: { url: { type: "string" } },
      required: ["url"],
    },
  }],
}));

mcpServer.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "browser_navigate") return { content: [{ type: "text", text: "Outil inconnu" }], isError: true };
  const { url } = request.params.arguments || {};
  const page = await context.newPage();
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
    return { content: [{ type: "text", text: `Succès : ${await page.title()}` }] };
  } catch (e) {
    return { content: [{ type: "text", text: `Erreur : ${e.message}` }], isError: true };
  } finally {
    await page.close();
  }
});

// Endpoint de santé indispensable pour Kubernetes
app.get("/health", (req, res) => res.status(200).send("OK"));

// Endpoint MCP Unifié
app.post("/mcp", async (req, res) => {
  try {
    const response = await mcpServer.handleRequest(req.body);
    res.json(response);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(8933, "0.0.0.0", () => console.log(`📡 MCP sur port 8933`));
