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

app = FastAPI(title="Playwright Wrapper Optimized", version="1.2.0")

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
    """Attend que le port soit ouvert avec un timeout de 5 minutes pour Kind."""
    deadline = time.time() + timeout
    log.info(f"Attente réseau pour {host}:{port}...")
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            log.info(f"Connexion réussie sur {host}:{port}")
            return
        except:
            await asyncio.sleep(5.0)
    raise TimeoutError(f"Pod {host} non prêt après {timeout}s")

async def _gc_loop():
    """Nettoie les pods et services expirés toutes les 30 secondes."""
    while True:
        await asyncio.sleep(30)
        ttl = float(settings.session_ttl_minutes) * 60
        now = time.time()
        expired = [sid for sid, si in SESSIONS.items() if (now - si.last_access) > ttl]
        
        for sid in expired:
            si = SESSIONS.pop(sid, None)
            if si:
                log.info(f"GC: Nettoyage de la session {sid} (Pod: {si.pod_name})")
                try:
                    COREV1.delete_namespaced_service(si.service_name, si.namespace)
                    COREV1.delete_namespaced_pod(si.pod_name, si.namespace)
                except Exception as e:
                    log.error(f"Erreur GC sur {si.pod_name}: {e}")

async def _ensure_mcp_initialized(si: SessionInfo):
    """Initialise le protocole MCP sur le serveur distant si nécessaire."""
    if si.mcp_session_id:
        return
    async with MCP_INIT_LOCKS.setdefault(si.session_id, asyncio.Lock()):
        if si.mcp_session_id:
            return
        log.info(f"Initialisation MCP pour {si.pod_name}...")
        async with httpx.AsyncClient(timeout=30.0) as c:
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
                    headers={
                        "host": UPSTREAM_HOST_HEADER,
                        "accept": "application/json, text/event-stream"
                    }
                )
                si.mcp_session_id = r.headers.get("mcp-session-id")
                log.info(f"MCP prêt. Session-ID: {si.mcp_session_id}")
            except Exception as e:
                log.error(f"Echec initialisation MCP: {e}")

async def proxy_any(session_id: str, path: str, request: Request):
    """Proxy générique vers le pod Playwright."""
    si = SESSIONS.get(session_id)
    if not si:
        raise HTTPException(status_code=404, detail="Session non trouvée ou expirée")
    
    si.last_access = time.time()
    
    # Préparation des headers (Fix 406 Not Acceptable)
    headers = dict(request.headers)
    headers.update({
        "host": UPSTREAM_HOST_HEADER,
        "accept": "application/json, text/event-stream",
        "connection": "keep-alive"
    })
    # Supprimer les headers de hop-by-hop qui posent problème
    for h in ("content-length", "content-type", "connection"):
        headers.pop(h, None)

    # Assurer l'initialisation MCP si on tape sur /mcp
    if "mcp" in path:
        await _ensure_mcp_initialized(si)
    
    if si.mcp_session_id:
        headers["mcp-session-id"] = si.mcp_session_id

    client_timeout = httpx.Timeout(120.0, connect=60.0)
    
    async def _make_req(client):
        return await client.request(
            method=request.method,
            url=f"{si.target_url}/{path}",
            headers=headers,
            content=await request.body(),
            params=request.query_params
        )

    # Gestion spécifique SSE (Server Sent Events)
    if "text/event-stream" in request.headers.get("accept", ""):
        client_sse = httpx.AsyncClient(timeout=client_timeout)
        req_sse = await _make_req(client_sse)
        
        async def _gen():
            try:
                async for chunk in req_sse.aiter_bytes():
                    yield chunk
            finally:
                await req_sse.aclose()
                await client_sse.aclose()
        return StreamingResponse(_gen(), status_code=req_sse.status_code, media_type="text/event-stream")
    
    # Gestion HTTP standard
    async with httpx.AsyncClient(timeout=client_timeout) as client_http:
        r = await _make_req(client_http)
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers={k: v for k, v in r.headers.items() if k.lower() != 'content-length'}
        )

@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_entry(request: Request, path: str = "mcp"):
    # Nettoyage de la clé de session pour n8n (évite les bugs {{ $execution.id }})
    raw_key = request.query_params.get("session") or "default"
    key = raw_key.replace("{", "").replace("}", "").replace("$", "").replace(".", "").strip()
    
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if not sid or sid not in SESSIONS:
            async with CREATE_SESSION_LOCK:
                # Vérification après lock pour éviter les doublons
                sid = STICKY_BY_KEY.get(key)
                if not sid or sid not in SESSIONS:
                    session_id = secrets.token_hex(8)
                    p_name = f"pw-{session_id}"
                    
                    # Limites de ressources pour protéger Kind
                    resources = client.V1ResourceRequirements(
                        requests={"cpu": "100m", "memory": "256Mi"},
                        limits={"cpu": "500m", "memory": "1Gi"}
                    )
                    
                    container = client.V1Container(
                        name="playwright",
                        image=settings.playwright_image,
                        ports=[client.V1ContainerPort(container_port=8933)],
                        resources=resources
                    )
                    
                    pod = client.V1Pod(
                        metadata=client.V1ObjectMeta(name=p_name, labels={"app": "pw", "sid": session_id}),
                        spec=client.V1PodSpec(containers=[container], restart_policy="Never")
                    )
                    
                    log.info(f"Démarrage d'un nouveau Pod Playwright: {p_name} (Session: {key})")
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
                        await _tcp_wait(f"{p_name}.n8n-prod.svc", 8933)
                    except TimeoutError:
                        log.error(f"Timeout sur le Pod {p_name}. Nettoyage...")
                        try:
                            COREV1.delete_namespaced_pod(p_name, "n8n-prod")
                        except: pass
                        raise HTTPException(status_code=504, detail="Le pod Playwright a mis trop de temps à démarrer.")
                    
                    target = f"http://{p_name}.n8n-prod.svc:8933"
                    si = SessionInfo(session_id=session_id, namespace="n8n-prod", 
                                     pod_name=p_name, service_name=p_name, target_url=target)
                    SESSIONS[session_id] = si
                    STICKY_BY_KEY[key] = session_id
                    sid = session_id

    return await proxy_any(sid, path, request)

@app.on_event("startup")
async def startup():
    log.info("Wrapper démarré.")
    if KUBE_AVAILABLE:
        asyncio.create_task(_gc_loop())

@app.get("/health")
async def health():
    return {"status": "ok", "sessions_active": len(SESSIONS)}
