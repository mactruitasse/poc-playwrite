const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { ListToolsRequestSchema, CallToolRequestSchema } = require('@modelcontextprotocol/sdk/types.js');
const express = require('express');

const app = express();
app.use(express.json());

const s = new Server(
  { name: 'n8n-worker', version: '1.0.0' },
  { capabilities: { tools: {} } }
);

// Enregistrement de l'outil
s.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{ 
    name: 'navigate_to', 
    description: 'Naviguer vers une URL avec Playwright', 
    inputSchema: { type: 'object', properties: { url: { type: 'string' } } } 
  }]
}));

// Gestionnaire d'appel (à remplir avec ta logique Playwright plus tard)
s.setRequestHandler(CallToolRequestSchema, async (request) => {
  return { content: [{ type: "text", text: "Action Playwright exécutée" }] };
});

app.post('/mcp', async (req, res) => {
  try {
    const { method, id } = req.body;
    const handler = s._requestHandlers.get(method);

    if (!handler) {
      return res.status(404).json({ jsonrpc: "2.0", id, error: { message: "Method not found" } });
    }

    const result = await handler(req.body);
    res.json({ jsonrpc: "2.0", id, result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(8080, '0.0.0.0', () => {
  console.log("MCP Server ready on port 8080");
});
