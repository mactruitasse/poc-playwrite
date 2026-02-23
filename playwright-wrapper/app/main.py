import asyncio
import logging
import os
import secrets
import time
import shlex
import json
from urllib.parse import urljoin

from dataclasses import dataclass, field
from typing import Dict, Optional, List

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Header
from fastapi.responses import StreamingResponse
from kubernetes import client, config
from app.settings import settings

log = logging.getLogger(__name__)
app = FastAPI(title="Playwright Wrapper (n8n Optimized)", version="1.1.1")

# --- Modèles & État ---
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

SESSIONS: Dict[str, SessionInfo] = {}
STICKY_BY_KEY: Dict[str, str] = {}
STICKY_LOCK = asyncio.Lock()
CREATE_SESSION_LOCK = asyncio.Lock()
MCP_INIT_LOCKS: Dict[str, asyncio.Lock] = {}

# --- K8s Setup ---
def _load_kube():
    try:
        if os.getenv("KUBERNETES_SERVICE_HOST"): config.load_incluster_config()
        else: config.load_kube_config()
        return True
    except: return False

KUBE_AVAILABLE = _load_kube()
COREV1 = client.CoreV1Api() if KUBE_AVAILABLE else None
UPSTREAM_HOST_HEADER = os.getenv("UPSTREAM_HOST_HEADER", "localhost")

async def _tcp_wait(host: str, port: int, timeout: float = 120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except: await asyncio.sleep(2.0)
    raise TimeoutError(f"Pod {host} non prêt")

async def _gc_loop():
    while True:
        await asyncio.sleep(30)
        ttl = float(settings.session_ttl_minutes) * 60
        now = time.time()
        expired = [sid for sid, si in SESSIONS.items() if (now - si.last_access) > ttl]
        for sid in expired:
            si = SESSIONS.pop(sid, None)
            if si:
                try:
                    COREV1.delete_namespaced_service(si.service_name, si.namespace)
                    COREV1.delete_namespaced_pod(si.pod_name, si.namespace)
                    log.info(f"GC: Pod {si.pod_name} supprimé")
                except: pass

# --- Logique Proxy n8n ---

async def _ensure_mcp_initialized(si: SessionInfo):
    if si.mcp_session_id: return
    async with MCP_INIT_LOCKS.setdefault(si.session_id, asyncio.Lock()):
        if si.mcp_session_id: return
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(f"{si.target_url}/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name":"w","version":"1"}, "capabilities":{}}
            }, headers={"host": UPSTREAM_HOST_HEADER, "accept": "application/json, text/event-stream"})
            si.mcp_session_id = r.headers.get("mcp-session-id")

async def proxy_any(session_id: str, path: str, request: Request):
    si = SESSIONS.get(session_id)
    if not si: raise HTTPException(status_code=404)
    si.last_access = time.time()

    # FORCE HEADERS POUR EVITER 406
    headers = dict(request.headers)
    headers.update({
        "host": UPSTREAM_HOST_HEADER,
        "accept": "application/json, text/event-stream",
        "connection": "keep-alive"
    })
    for h in ("content-length", "content-type"): headers.pop(h, None)

    is_mcp = "mcp" in path
    if is_mcp: await _ensure_mcp_initialized(si)
    if si.mcp_session_id: headers["mcp-session-id"] = si.mcp_session_id

    client_timeout = httpx.Timeout(60.0, connect=30.0)
    
    async def _make_req(c):
        return await c.request(request.method, f"{si.target_url}/{path}", 
                               headers=headers, content=await request.body())

    if "text/event-stream" in request.headers.get("accept", ""):
        c_sse = httpx.AsyncClient(timeout=client_timeout)
        r = await _make_req(c_sse)
        async def _gen():
            try:
                async for chunk in r.aiter_bytes(): yield chunk
            finally:
                await r.aclose()
                await c_sse.aclose()
        return StreamingResponse(_gen(), status_code=r.status_code, media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=client_timeout) as c_http:
            r = await _make_req(c_http)
            return Response(content=r.content, status_code=r.status_code, 
                            headers={k:v for k,v in r.headers.items() if k.lower() != 'content-length'})

# --- Routes ---

@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_entry(request: Request, path: str = "mcp"):
    raw_key = request.query_params.get("session") or "default"
    # Nettoyage pour éviter les bugs d'expression n8n
    key = raw_key.replace("{", "").replace("}", "").replace("$", "").strip()
    
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if not sid or sid not in SESSIONS:
            async with CREATE_SESSION_LOCK:
                sid = STICKY_BY_KEY.get(key)
                if not sid or sid not in SESSIONS:
                    session_id = secrets.token_hex(8)
                    # Création K8s simplifiée (réutilise ta logique de création ici)
                    p_name = f"pw-{session_id}"
                    container = client.V1Container(name="playwright", image=settings.playwright_image, 
                                                   ports=[client.V1ContainerPort(container_port=8933)])
                    pod = client.V1Pod(metadata=client.V1ObjectMeta(name=p_name, labels={"app":"pw","sid":session_id}),
                                       spec=client.V1PodSpec(containers=[container], restart_policy="Never"))
                    COREV1.create_namespaced_pod(namespace="n8n-prod", body=pod)
                    
                    svc = client.V1Service(metadata=client.V1ObjectMeta(name=p_name, labels={"app":"pw","sid":session_id}),
                                           spec=client.V1ServiceSpec(selector={"app":"pw","sid":session_id}, 
                                                                    ports=[client.V1ServicePort(port=8933)]))
                    COREV1.create_namespaced_service(namespace="n8n-prod", body=svc)
                    
                    target = f"http://{p_name}.n8n-prod.svc:8933"
                    await _tcp_wait(f"{p_name}.n8n-prod.svc", 8933)
                    
                    si = SessionInfo(session_id=session_id, namespace="n8n-prod", 
                                     pod_name=p_name, service_name=p_name, target_url=target)
                    SESSIONS[session_id] = si
                    STICKY_BY_KEY[key] = session_id
                    sid = session_id
    return await proxy_any(sid, path, request)

@app.on_event("startup")
async def startup():
    if KUBE_AVAILABLE: asyncio.create_task(_gc_loop())

@app.get("/health")
async def health(): return {"ok": True}
