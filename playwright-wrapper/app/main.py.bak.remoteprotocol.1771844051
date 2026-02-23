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
from kubernetes.client.rest import ApiException

from app.settings import settings

# --- Configuration & Initialisation ---
log = logging.getLogger(__name__)
logger = log

app = FastAPI(title="Playwright Wrapper (Transparent Proxy)", version="1.1.0")

@app.get("/health")
async def health():
    return {"ok": True}

# --- Utilitaires de Configuration ---

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _split_args(s: Optional[str]) -> Optional[List[str]]:
    if s is None: return None
    s = s.strip()
    if not s: return None
    return shlex.split(s)

def _load_kube() -> bool:
    if os.getenv("KUBE_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        log.info("Kubernetes disabled via KUBE_ENABLED=false")
        return False
    in_cluster = bool(os.getenv("KUBERNETES_SERVICE_HOST")) and bool(os.getenv("KUBERNETES_SERVICE_PORT"))
    try:
        if in_cluster:
            config.load_incluster_config()
            log.info("Loaded in-cluster Kubernetes config")
        else:
            config.load_kube_config()
            log.info("Loaded kubeconfig (out-of-cluster)")
        return True
    except Exception as e:
        log.warning("Failed to load Kubernetes config: %s", e)
        return False

def _configure_logging():
    level = (settings.log_level or "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# --- Modèles et État Global ---

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
MCP_INIT_LOCKS: Dict[str, asyncio.Lock] = {}
CREATE_SESSION_LOCK = asyncio.Lock() # Bloque la création simultanée de pods

KUBE_AVAILABLE = _load_kube()
COREV1 = client.CoreV1Api() if KUBE_AVAILABLE else None

KEEP_FAILED_RESOURCES = _env_bool("KEEP_FAILED_RESOURCES", False)
UPSTREAM_HOST_HEADER = os.getenv("UPSTREAM_HOST_HEADER", "localhost")
MCP_AUTO_INIT = _env_bool("MCP_AUTO_INIT", True)

# --- Logique Kubernetes (PVC, Specs, Sécurité) ---

def _get_namespace() -> str:
    if settings.target_namespace: return settings.target_namespace
    ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    if os.path.exists(ns_path):
        try: return open(ns_path, "r", encoding="utf-8").read().strip()
        except: pass
    return "default"

def _build_security_contexts():
    container_security_ctx = None
    pod_security_ctx = None
    if settings.run_as_non_root:
        container_security_ctx = client.V1SecurityContext(
            run_as_non_root=True, run_as_user=settings.run_as_user,
            run_as_group=settings.run_as_group, allow_privilege_escalation=settings.allow_privilege_escalation,
            read_only_root_filesystem=settings.read_only_root_filesystem,
        )
        if settings.drop_all_caps:
            container_security_ctx.capabilities = client.V1Capabilities(drop=["ALL"])
    if settings.fs_group:
        pod_security_ctx = client.V1PodSecurityContext(fs_group=settings.fs_group)
    if settings.seccomp_runtime_default:
        if container_security_ctx is None: container_security_ctx = client.V1SecurityContext()
        container_security_ctx.seccomp_profile = client.V1SeccompProfile(type="RuntimeDefault")
    return container_security_ctx, pod_security_ctx

def _pvc_volumes():
    if not settings.enable_pvc_mount: return None, None
    vol = client.V1Volume(name="artifacts", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=settings.pvc_name))
    vm = client.V1VolumeMount(name="artifacts", mount_path=settings.pvc_mount_path)
    return [vol], [vm]

def _playwright_container_spec() -> client.V1Container:
    cmd = (settings.playwright_command or "").strip() or None
    args = _split_args(settings.playwright_args)
    return client.V1Container(
        name="playwright", image=settings.playwright_image, image_pull_policy="IfNotPresent",
        command=[cmd] if cmd else None, args=args,
        ports=[client.V1ContainerPort(container_port=settings.playwright_port)],
    )

# --- Gestion des Sessions & MCP ---

def _httpx_timeouts(read_timeout: float) -> httpx.Timeout:
    rt = None if (read_timeout or 0) <= 0 else float(read_timeout)
    return httpx.Timeout(
        connect=settings.http_connect_timeout_seconds,
        read=rt, write=settings.sse_write_timeout_seconds,
        pool=settings.http_connect_timeout_seconds,
    )

async def _ensure_mcp_initialized(si: SessionInfo) -> None:
    if si.mcp_session_id: return
    lock = MCP_INIT_LOCKS.setdefault(si.session_id, asyncio.Lock())
    async with lock:
        if si.mcp_session_id: return
        log.info(f"Initializing MCP for session {si.session_id}")
        upstream = f"{si.target_url.rstrip('/')}/mcp"
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", "host": UPSTREAM_HOST_HEADER}
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "playwright-wrapper", "version": "1.1.0"}, "capabilities": {}}
        }
        async with httpx.AsyncClient(timeout=_httpx_timeouts(settings.http_default_read_timeout_seconds)) as client_http:
            r = await client_http.post(upstream, headers=headers, json=payload)
            sid = r.headers.get("mcp-session-id")
            if not sid:
                raise HTTPException(status_code=502, detail=f"MCP init failed: {r.text[:200]}")
            si.mcp_session_id = sid.strip()

async def _tcp_wait(host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except:
            await asyncio.sleep(1.0)
    raise TimeoutError(f"TCP connect to {host}:{port} timed out")

async def _create_pod_and_service(session_id: str, namespace: str) -> SessionInfo:
    pod_name, svc_name = f"pw-{session_id}", f"pw-{session_id}"
    c_sec, p_sec = _build_security_contexts()
    c = _playwright_container_spec()
    c.security_context = c_sec
    vols, vms = _pvc_volumes()
    if vms: c.volume_mounts = vms

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, labels={"app": "pw", "sid": session_id}),
        spec=client.V1PodSpec(containers=[c], restart_policy="Never", security_context=p_sec, volumes=vols)
    )
    COREV1.create_namespaced_pod(namespace=namespace, body=pod)
    
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=svc_name, labels={"app": "pw", "sid": session_id}),
        spec=client.V1ServiceSpec(selector={"app": "pw", "sid": session_id}, 
                                  ports=[client.V1ServicePort(port=settings.playwright_port)],
                                  type=(settings.service_type or "ClusterIP"))
    )
    COREV1.create_namespaced_service(namespace=namespace, body=svc)

    target_url = f"http://{svc_name}.{namespace}.svc:{settings.playwright_port}"
    await _tcp_wait(f"{svc_name}.{namespace}.svc", settings.playwright_port, settings.pod_ready_timeout_seconds)
    return SessionInfo(session_id=session_id, namespace=namespace, pod_name=pod_name, service_name=svc_name, target_url=target_url)

@app.post("/sessions")
async def create_session():
    if not KUBE_AVAILABLE: raise HTTPException(status_code=500, detail="K8s API unavailable")
    session_id = secrets.token_hex(8)
    si = await _create_pod_and_service(session_id, _get_namespace())
    if MCP_AUTO_INIT: await _ensure_mcp_initialized(si)
    SESSIONS[session_id] = si
    return {"sessionId": session_id, "targetUrl": si.target_url}

async def _get_or_create_sticky_session_id_for(key: str) -> str:
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if sid and sid in SESSIONS: return sid
        async with CREATE_SESSION_LOCK: # Un seul pod créé à la fois
            sid = STICKY_BY_KEY.get(key)
            if sid and sid in SESSIONS: return sid
            data = await create_session()
            sid = data["sessionId"]
            STICKY_BY_KEY[key] = sid
            return sid

# --- Proxy Engine ---

def _is_mcp_path(path: str) -> bool:
    p = (path or "").lstrip("/")
    return p == "mcp" or p.startswith("mcp/")

def _strip_hop_by_hop(resp_headers) -> dict:
    hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "content-length"}
    return {k: v for k, v in resp_headers.items() if k.lower() not in hop}

async def proxy_any(session_id: str, path: str, request: Request):
    si = SESSIONS.get(session_id)
    if not si: raise HTTPException(status_code=404, detail="Session lost")
    si.last_access = time.time()

    upstream = f"{si.target_url.rstrip('/')}/{path.lstrip('/')}"
    method = request.method.upper()
    headers = dict(request.headers)
    headers["host"] = UPSTREAM_HOST_HEADER
    for h in ("connection", "content-length"): headers.pop(h, None)

    body = await request.body() if method not in ("GET", "HEAD") else b""
    is_mcp = _is_mcp_path(path)
    
    if is_mcp:
        headers["accept"] = "application/json, text/event-stream"
        await _ensure_mcp_initialized(si)
        if si.mcp_session_id: headers["mcp-session-id"] = si.mcp_session_id

    async def _handle_request(client_http, stream=False):
        r = await client_http.request(method, upstream, headers=headers, content=body)
        if is_mcp and r.status_code == 409: # Correction du conflit n8n
            log.warning(f"MCP Conflict 409 for {session_id}, retrying...")
            si.mcp_session_id = None
            await _ensure_mcp_initialized(si)
            headers["mcp-session-id"] = si.mcp_session_id
            return await client_http.request(method, upstream, headers=headers, content=body)
        return r

    is_sse = "text/event-stream" in headers.get("accept", "").lower()
    tout = _httpx_timeouts(settings.sse_read_timeout_seconds if is_sse else settings.http_default_read_timeout_seconds)

    try:
        if is_sse:
            client_http = httpx.AsyncClient(timeout=tout)
            r = await _handle_request(client_http, stream=True)
            
            async def _stream_gen():
                try:
                    async for chunk in r.aiter_bytes(): yield chunk
                finally:
                    await r.aclose()
                    await client_http.aclose()
            
            return StreamingResponse(_stream_gen(), status_code=r.status_code, media_type="text/event-stream", headers=_strip_hop_by_hop(r.headers))
        else:
            async with httpx.AsyncClient(timeout=tout) as client_http:
                r = await _handle_request(client_http)
                return Response(content=r.content, status_code=r.status_code, headers=_strip_hop_by_hop(r.headers))
    except Exception as e:
        log.error(f"Proxy Error: {e}")
        raise HTTPException(status_code=502, detail=str(e))

# --- Routes API ---

@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_route(request: Request, path: str = "mcp"):
    key = request.query_params.get("session") or request.query_params.get("workflowId") or "default"
    sid = await _get_or_create_sticky_session_id_for(key)
    return await proxy_any(sid, path, request)

@app.on_event("startup")
async def startup():
    _configure_logging()
    log.info("Proxy version 1.1.0 started")
