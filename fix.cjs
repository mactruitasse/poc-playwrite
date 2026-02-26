const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { ListToolsRequestSchema, CallToolRequestSchema } = require('@modelcontextprotocol/sdk/types.js');
const { chromium } = require('playwright');
const express = require('express');

const app = express();
app.use(express.json());

const s = new Server(
  { name: 'n8n-playwright-worker', version: '1.0.0' },
  { capabilities: { tools: {} } }
);

// 1. Liste des outils disponibles
s.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{ 
    name: 'navigate_to', 
    description: 'Navigue vers une URL et récupère le titre et le contenu texte', 
    inputSchema: { 
      type: 'object', 
      properties: { 
        url: { type: 'string', description: 'L\'URL complète (https://...)' } 
      },
      required: ['url']
    } 
  }]
}));

// 2. Logique d'exécution Playwright
s.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name === "navigate_to") {
    const url = request.params.arguments.url;
    console.log(`Navigation vers : ${url}`);
    
    // On lance Chromium avec des arguments de sécurité pour Docker/Kubernetes
    const browser = await chromium.launch({ 
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox'] 
    });

    try {
      const page = await browser.newPage();
      await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
      
      const title = await page.title();
      const text = await page.evaluate(() => document.body.innerText.slice(0, 500)); // On prend les 500 premiers caractères
      
      await browser.close();
      return { 
        content: [{ 
          type: "text", 
          text: `Navigation réussie sur "${title}".\n\nExtrait du contenu :\n${text}...` 
        }] 
      };
    } catch (error) {
      await browser.close();
      return {
        content: [{ type: "text", text: `Erreur Playwright : ${error.message}` }],
        isError: true
      };
    }
  }
});

// 3. Tunnel de communication HTTP
app.post('/mcp', async (req, res) => {
  try {
    const { method, id } = req.body;
    const handler = s._requestHandlers.get(method);
    if (!handler) return res.status(404).json({ jsonrpc: "2.0", id, error: { message: "Method not found" } });
    
    const result = await handler(req.body);
    res.json({ jsonrpc: "2.0", id, result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(8080, '0.0.0.0', () => {
  console.log("🚀 Worker Playwright prêt sur le port 8080");
});
