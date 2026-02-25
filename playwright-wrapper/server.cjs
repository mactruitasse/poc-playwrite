const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { 
  ListToolsRequestSchema, 
  CallToolRequestSchema 
} = require("@modelcontextprotocol/sdk/types.js");
const express = require("express");

const app = express();
app.use(express.json());

// 1. Initialisation du serveur MCP
const server = new Server(
  {
    name: "n8n-playwright-worker",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

/**
 * 2. Définition des outils (Tools)
 * Ajoute ici tes outils Playwright personnalisés
 */
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "navigate_to",
        description: "Naviguer vers une URL avec Playwright",
        inputSchema: {
          type: "object",
          properties: {
            url: { type: "string" },
          },
          required: ["url"],
        },
      }
    ],
  };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (name === "navigate_to") {
    // Logique Playwright ici
    return {
      content: [{ type: "text", text: `Navigation vers ${args.url} réussie` }],
    };
  }
  throw new Error(`Outil non trouvé: ${name}`);
});

/**
 * 3. Endpoint POST /mcp
 * C'est ici qu'on remplace mcpServer.handleRequest par server.handlePayload
 */
app.post("/mcp", async (req, res) => {
  try {
    // handlePayload est la méthode standard pour traiter le JSON-RPC reçu via HTTP
    const response = await server.handlePayload(req.body);
    res.json(response);
  } catch (error) {
    console.error("Erreur MCP:", error);
    res.status(500).json({
      jsonrpc: "2.0",
      id: req.body.id || null,
      error: {
        code: -32603,
        message: error.message,
      },
    });
  }
});

// Endpoint de santé (Healthcheck)
app.get("/health", (req, res) => {
  res.status(200).send("OK");
});

// Démarrage du serveur sur le port 8080 (cohérent avec tes logs Flask/K8s)
const PORT = process.env.PORT || 8080;
app.listen(PORT, "0.0.0.0", () => {
  console.log(`Serveur MCP Playwright démarré sur http://0.0.0.0:${PORT}`);
});
