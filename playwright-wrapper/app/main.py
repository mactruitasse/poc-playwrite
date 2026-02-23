import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from kubernetes import client, config
from app.settings import settings

# Configuration du logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Playwright Wrapper Survival Mode", version="1.5.0")

@dataclass
class SessionInfo:
    session_id: str
    namespace: str
    created_at: float = field(default_factory=lambda: time.time())
    pod_name: Optional[str] = None
    service_name: Optional[str] = None
    target_url: Optional[str] = None
    last_access: float = field(default_factory=lambda: time.time())
    mcp_session_id: Optional[str] = None

# État global
SESSIONS: Dict[str, SessionInfo] = {}
STICKY_BY_KEY: Dict[str, str] = {}
STICKY_LOCK = asyncio.Lock()
CREATE_SESSION_LOCK = asyncio.Lock()
MCP_INIT_LOCKS: Dict[str, asyncio.Lock] = {}

# Initialisation Kubernetes
def _load_kube():
    try:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
        else:
            config.load_kube_config()
        return True
    except Exception as e:
        log.error(f"Erreur Kube Config: {e}")
        return False

KUBE_AVAILABLE = _load_kube()
COREV1 = client.CoreV1Api() if KUBE_AVAILABLE else None
UPSTREAM_HOST_HEADER = os.getenv("UPSTREAM_HOST_HEADER", "localhost")

async def _tcp_wait(host: str, port: int, timeout: float = 300.0):
    """Attend que le port soit ouvert."""
    deadline = time.time() + timeout
    log.info(f"Attente réseau pour {host}:{port} (max {timeout}s)...")
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
            writer.close()
            await writer.wait_closed()
            log.info(f"SUCCESS: Pod {host} répond sur le port {port}")
            return
        except:
            await asyncio.sleep(2.0)
    raise TimeoutError(f"Le pod {host} n'a jamais ouvert le port {port}")

async def _ensure_mcp_initialized(si: SessionInfo):
    """Initialise le protocole MCP une fois le pod prêt."""
    if si.mcp_session_id:
        return
    async with MCP_INIT_LOCKS.setdefault(si.session_id, asyncio.Lock()):
        if si.mcp_session_id:
            return
        log.info(f"Initialisation MCP pour {si.pod_name}...")
        async with httpx.AsyncClient(timeout=60.0) as c:
            try:
                r = await c.post(
                    f"{si.target_url}/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "clientInfo": {"name": "n8n-wrapper", "version": "1.0"},
                            "capabilities": {}
                        }
                    },
                    headers={"host": UPSTREAM_HOST_HEADER, "accept": "application/json"}
                )
                si.mcp_session_id = r.headers.get("mcp-session-id")
                log.info(f"Initialisation MCP OK pour {si.pod_name}")
            except Exception as e:
                log.error(f"Erreur init MCP sur {si.pod_name}: {e}")

async def proxy_any(session_id: str, path: str, request: Request):
    si = SESSIONS.get(session_id)
    if not si:
        raise HTTPException(status_code=404, detail="Session non trouvée")
    
    si.last_access = time.time()
    headers = dict(request.headers)
    headers.update({"host": UPSTREAM_HOST_HEADER})
    
    for h in ("content-length", "host", "connection"):
        headers.pop(h, None)

    if "mcp" in path:
        await _ensure_mcp_initialized(si)
    
    if si.mcp_session_id:
        headers["mcp-session-id"] = si.mcp_session_id

    async with httpx.AsyncClient(timeout=120.0) as client_http:
        try:
            r = await client_http.request(
                method=request.method,
                url=f"{si.target_url}/{path}",
                headers=headers,
                content=await request.body(),
                params=request.query_params
            )
            return Response(content=r.content, status_code=r.status_code, headers=dict(r.headers))
        except Exception as e:
            log.error(f"Proxy Error: {e}")
            raise HTTPException(status_code=502, detail="Navigateur injoignable")

@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_entry(request: Request, path: str = "mcp"):
    raw_key = request.query_params.get("session") or "default"
    key = raw_key.replace("{", "").replace("}", "").replace("$", "").strip() or "default"
    
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if not sid or sid not in SESSIONS:
            async with CREATE_SESSION_LOCK:
                sid = STICKY_BY_KEY.get(key)
                if not sid or sid not in SESSIONS:
                    session_id = secrets.token_hex(8)
                    p_name = f"pw-{session_id}"
                    
                    resources = client.V1ResourceRequirements(
                        requests={"cpu": "100m", "memory": "256Mi"},
                        limits={"cpu": "500m", "memory": "512Mi"}
                    )
                    
                    # CONFIGURATION CORRIGÉE DU CONTAINER
                    container = client.V1Container(
                        name="playwright",
                        image=settings.playwright_image,
                        # Notez le 's' à modelcontextprotocols
                        command=["npx", "-y", "@modelcontextprotocols/server-playwright"],
                        args=["--port", "8933"],
                        ports=[client.V1ContainerPort(container_port=8933)],
                        resources=resources,
                        env=[
                            client.V1EnvVar(name="PORT", value="8933"),
                            # Redirige le cache npm vers /tmp car le container est non-root
                            client.V1EnvVar(name="npm_config_cache", value="/tmp/.npm"),
                            client.V1EnvVar(name="XDG_CACHE_HOME", value="/tmp/.cache"),
                            client.V1EnvVar(name="HOME", value="/tmp")
                        ]
                    )
                    
                    pod = client.V1Pod(
                        metadata=client.V1ObjectMeta(name=p_name, labels={"app": "pw", "sid": session_id}),
                        spec=client.V1PodSpec(containers=[container], restart_policy="Never")
                    )
                    
                    log.info(f"Démarrage d'un nouveau Pod Playwright: {p_name}")
                    COREV1.create_namespaced_pod(namespace="n8n-prod", body=pod)
                    
                    svc = client.V1Service(
                        metadata=client.V1ObjectMeta(name=p_name),
                        spec=client.V1ServiceSpec(
                            selector={"app": "pw", "sid": session_id},
                            ports=[client.V1ServicePort(port=8933)]
                        )
                    )
                    COREV1.create_namespaced_service(namespace="n8n-prod", body=svc)
                    
                    target = f"http://{p_name}.n8n-prod.svc:8933"
                    
                    try:
                        await _tcp_wait(f"{p_name}.n8n-prod.svc", 8933)
                    except Exception as e:
                        log.error(f"Timeout sur {p_name}: {e}")
                        raise HTTPException(status_code=504, detail="Pod Playwright non prêt")
                    
                    SESSIONS[session_id] = SessionInfo(
                        session_id=session_id, 
                        namespace="n8n-prod", 
                        pod_name=p_name, 
                        target_url=target
                    )
                    STICKY_BY_KEY[key] = session_id
                    sid = session_id

    return await proxy_any(sid, path, request)

@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(SESSIONS)}

@app.on_event("startup")
async def startup_event():
    log.info("Wrapper Playwright prêt.")
