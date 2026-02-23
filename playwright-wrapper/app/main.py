import asyncio
import logging
import os
import secrets
import time
import json
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from kubernetes import client, config
from app.settings import settings

# Configuration du logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Playwright Wrapper Survival Mode", version="1.3.0")

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
    """Attend que le port soit ouvert avec un timeout très long pour Kind."""
    deadline = time.time() + timeout
    log.info(f"Attente réseau pour {host}:{port} (max {timeout}s)...")
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            log.info(f"SUCCESS: Pod {host} répond sur le port {port}")
            return
        except:
            await asyncio.sleep(5.0) # On ne bombarde pas le CPU
    raise TimeoutError(f"Pod {host} toujours injoignable après {timeout}s")

# GC DÉSACTIVÉ TEMPORAIREMENT POUR ÉVITER LA SUPPRESSION PRÉMATURÉE
async def _gc_loop():
    log.info("Garbage Collector en pause pour laisser les pods démarrer.")
    return 

async def _ensure_mcp_initialized(si: SessionInfo):
    """Initialise le protocole MCP sur le serveur Playwright."""
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
        raise HTTPException(status_code=404, detail="Session perdue")
    
    si.last_access = time.time()
    
    headers = dict(request.headers)
    headers.update({
        "host": UPSTREAM_HOST_HEADER,
        "accept": "application/json, text/event-stream",
        "connection": "keep-alive"
    })
    for h in ("content-length", "content-type", "connection"):
        headers.pop(h, None)

    if "mcp" in path:
        await _ensure_mcp_initialized(si)
    
    if si.mcp_session_id:
        headers["mcp-session-id"] = si.mcp_session_id

    client_timeout = httpx.Timeout(180.0, connect=60.0)
    
    async with httpx.AsyncClient(timeout=client_timeout) as client_http:
        try:
            r = await client_http.request(
                method=request.method,
                url=f"{si.target_url}/{path}",
                headers=headers,
                content=await request.body(),
                params=request.query_params
            )
            return Response(
                content=r.content,
                status_code=r.status_code,
                headers={k: v for k, v in r.headers.items() if k.lower() != 'content-length'}
            )
        except Exception as e:
            log.error(f"Erreur Proxy vers {si.pod_name}: {e}")
            raise HTTPException(status_code=502, detail="Erreur de communication avec le navigateur")

@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_entry(request: Request, path: str = "mcp"):
    raw_key = request.query_params.get("session") or "default"
    # Nettoyage profond pour Kind/n8n
    key = raw_key.replace("{", "").replace("}", "").replace("$", "").replace(" ", "").strip()
    
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if not sid or sid not in SESSIONS:
            async with CREATE_SESSION_LOCK:
                sid = STICKY_BY_KEY.get(key)
                if not sid or sid not in SESSIONS:
                    session_id = secrets.token_hex(8)
                    p_name = f"pw-{session_id}"
                    
                    # Ressources MINI pour Kind
                    resources = client.V1ResourceRequirements(
                        requests={"cpu": "50m", "memory": "128Mi"},
                        limits={"cpu": "500m", "memory": "512Mi"}
                    )
                    
                    container = client.V1Container(
                        name="playwright",
                        image=settings.playwright_image,
                        ports=[client.V1ContainerPort(container_port=8933)],
                        resources=resources,
                        env=[client.V1EnvVar(name="MAX_CONCURRENT_SESSIONS", value="1")]
                    )
                    
                    pod = client.V1Pod(
                        metadata=client.V1ObjectMeta(name=p_name, labels={"app": "pw", "sid": session_id}),
                        spec=client.V1PodSpec(containers=[container], restart_policy="Never")
                    )
                    
                    log.info(f"CREATION POD: {p_name} pour session {key}")
                    COREV1.create_namespaced_pod(namespace="n8n-prod", body=pod)
                    
                    svc = client.V1Service(
                        metadata=client.V1ObjectMeta(name=p_name, labels={"app": "pw", "sid": session_id}),
                        spec=client.V1ServiceSpec(
                            selector={"app": "pw", "sid": session_id},
                            ports=[client.V1ServicePort(port=8933)]
                        )
                    )
                    COREV1.create_namespaced_service(namespace="n8n-prod", body=svc)
                    
                    try:
                        # On attend que le pod soit réellement prêt
                        await _tcp_wait(f"{p_name}.n8n-prod.svc", 8933)
                    except Exception as e:
                        log.error(f"ÉCHEC DÉMARRAGE {p_name}: {e}")
                        raise HTTPException(status_code=504, detail="Navigateur trop lent à démarrer")
                    
                    target = f"http://{p_name}.n8n-prod.svc:8933"
                    SESSIONS[session_id] = SessionInfo(session_id=session_id, namespace="n8n-prod", 
                                                       pod_name=p_name, service_name=p_name, target_url=target)
                    STICKY_BY_KEY[key] = session_id
                    sid = session_id

    return await proxy_any(sid, path, request)

@app.on_event("startup")
async def startup():
    log.info("Wrapper prêt en mode survie.")

@app.get("/health")
async def health():
    return {"status": "ok", "active_pods": len(SESSIONS)}
