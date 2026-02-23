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

# --- Configuration & Logging ---
log = logging.getLogger(__name__)
logger = log

app = FastAPI(title="Playwright Wrapper (Full Proxy + GC)", version="1.1.0")

def _configure_logging():
    level = (settings.log_level or "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# --- Modèles de données ---
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

# --- État Global & Verrous ---
SESSIONS: Dict[str, SessionInfo] = {}
STICKY_BY_KEY: Dict[str, str] = {}
STICKY_LOCK = asyncio.Lock()
MCP_INIT_LOCKS: Dict[str, asyncio.Lock] = {}
CREATE_SESSION_LOCK = asyncio.Lock() # Empêche la double création de pods

# --- Chargement Kubernetes ---
def _load_kube() -> bool:
    if os.getenv("KUBE_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        return False
    in_cluster = bool(os.getenv("KUBERNETES_SERVICE_HOST")) and bool(os.getenv("KUBERNETES_SERVICE_PORT"))
    try:
        if in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        return True
    except Exception as e:
        log.warning("Failed to load Kubernetes config: %s", e)
        return False

KUBE_AVAILABLE = _load_kube()
COREV1 = client.CoreV1Api() if KUBE_AVAILABLE else None
UPSTREAM_HOST_HEADER = os.getenv("UPSTREAM_HOST_HEADER", "localhost")
MCP_AUTO_INIT = os.getenv("MCP_AUTO_INIT", "true").lower() in ("1", "true", "yes")

# --- Utilitaires de Spec Kubernetes ---

def _get_namespace() -> str:
    if settings.target_namespace: return settings.target_namespace
    ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    if os.path.exists(ns_path):
        try: return open(ns_path, "r", encoding="utf-8").read().strip()
        except: pass
    return "default"

def _build_security_contexts():
    c_sec = None
    p_sec = None
    if settings.run_as_non_root:
        c_sec = client.V1SecurityContext(
            run_as_non_root=True, run_as_user=settings.run_as_user,
            run_as_group=settings.run_as_group, allow_privilege_escalation=settings.allow_privilege_escalation,
            read_only_root_filesystem=settings.read_only_root_filesystem,
        )
        if settings.drop_all_caps:
            c_sec.capabilities = client.V1Capabilities(drop=["ALL"])
    if settings.fs_group:
        p_sec = client.V1PodSecurityContext(fs_group=settings.fs_group)
    if settings.seccomp_runtime_default:
        if c_sec is None: c_sec = client.V1SecurityContext()
        c_sec.seccomp_profile = client.V1SeccompProfile(type="RuntimeDefault")
    return c_sec, p_sec

def _pvc_volumes():
    if not settings.enable_pvc_mount: return None, None
    vol = client.V1Volume(name="artifacts", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=settings.pvc_name))
    vm = client.V1VolumeMount(name="artifacts", mount_path=settings.pvc_mount_path)
    return [vol], [vm]

def _playwright_container_spec() -> client.V1Container:
    cmd = (settings.playwright_command or "").strip() or None
    args = shlex.split(settings.playwright_args) if settings.playwright_args else None
    return client.V1Container(
        name="playwright", image=settings.playwright_image, image_pull_policy="IfNotPresent",
        command=[cmd] if cmd else None, args=args,
        ports=[client.V1ContainerPort(container_port=settings.playwright_port)],
    )

# --- Cycle de vie (Creation & GC) ---

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
    raise TimeoutError(f"TCP wait on {host}:{port} timed out")

async def _create_pod_and_service(session_id: str, namespace: str) -> SessionInfo:
    p_name, s_name = f"pw-{session_id}", f"pw-{session_id}"
    c_sec, p_sec = _build_security_contexts()
    c = _playwright_container_spec()
    c.security_context = c_sec
    vols, vms = _pvc_volumes()
    if vms: c.volume_mounts = vms

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=p_name, labels={"app": "pw", "sid": session_id}),
        spec=client.V1PodSpec(containers=[c], restart_policy="Never", security_context=p_sec, volumes=vols)
    )
    COREV1.create_namespaced_pod(namespace=namespace, body=pod)
    
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=s_name, labels={"app": "pw", "sid": session_id}),
        spec=client.V1ServiceSpec(selector={"app": "pw", "sid": session_id}, 
                                  ports=[client.V1ServicePort(port=settings.playwright_port)],
                                  type=(settings.service_type or "ClusterIP"))
    )
    COREV1.create_namespaced_service(namespace=namespace, body=svc)

    target_url = f"http://{s_name}.{namespace}.svc:{settings.playwright_port}"
    await _tcp_wait(f"{s_name}.{namespace}.svc", settings.playwright_port, settings.pod_ready_timeout_seconds)
    return SessionInfo(session_id=session_id, namespace=namespace, pod_name=p_name, service_name=s_name, target_url=target_url)

async def _gc_loop():
    """Supprime les pods inactifs."""
    while True:
        try:
            await asyncio.sleep(settings.gc_interval_seconds)
            ttl_sec = float(settings.session_ttl_minutes) * 60.0
            now = time.time()
            expired = [sid for sid, si in SESSIONS.items() if (now - si.last_access) > ttl_sec]
            for sid in expired:
                log.info(f"GC: Cleaning up expired session {sid}")
                await delete_session(sid)
        except Exception as e:
            log.error(f"GC Loop error: {e}")

@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    si = SESSIONS.pop(session_id, None)
    if not si: return {"ok": True}
    try:
        COREV1.delete_namespaced_service(name=si.service_name, namespace=si.namespace)
        COREV1.delete_namespaced_pod(name=si.pod_name, namespace=si.namespace)
    except Exception as e:
        log.warning(f"Cleanup error for {session_id}: {e}")
    # Nettoyage des index collants
    for k, v in list(STICKY_BY_KEY.items()):
        if v == session_id: STICKY_BY_KEY.pop(k, None)
    return {"ok": True}

# --- Logique Proxy & MCP ---

async def _ensure_mcp_initialized(si: SessionInfo) -> None:
    if si.mcp_session_id: return
    lock = MCP_INIT_LOCKS.setdefault(si.session_id, asyncio.Lock())
    async with lock:
        if si.mcp_session_id: return
        upstream = f"{si.target_url.rstrip('/')}/mcp"
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", "host": UPSTREAM_HOST_HEADER}
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "playwright-wrapper", "version": "1.1.0"}, "capabilities": {}}
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client_http:
            r = await client_http.post(upstream, headers=headers, json=payload)
            sid = r.headers.get("mcp-session-id")
            if not sid: raise Exception("Failed to get MCP session ID")
            si.mcp_session_id = sid.strip()

async def proxy_any(session_id: str, path: str, request: Request):
    si = SESSIONS.get(session_id)
    if not si: raise HTTPException(status_code=404, detail="Session expired or not found")
    si.last_access = time.time()

    upstream = f"{si.target_url.rstrip('/')}/{path.lstrip('/')}"
    method = request.method.upper()
    headers = dict(request.headers)
    headers["host"] = UPSTREAM_HOST_HEADER
    for h in ("connection", "content-length"): headers.pop(h, None)
    
    body = await request.body() if method not in ("GET", "HEAD") else b""
    is_mcp = (path == "mcp" or path.startswith("mcp/"))

    if is_mcp:
        headers["accept"] = "application/json, text/event-stream"
        await _ensure_mcp_initialized(si)
        if si.mcp_session_id: headers["mcp-session-id"] = si.mcp_session_id

    async def _do_req(client_http):
        r = await client_http.request(method, upstream, headers=headers, content=body)
        if is_mcp and r.status_code == 409: # GESTION CONFLIT N8N
            si.mcp_session_id = None
            await _ensure_mcp_initialized(si)
            headers["mcp-session-id"] = si.mcp_session_id
            return await client_http.request(method, upstream, headers=headers, content=body)
        return r

    is_sse = "text/event-stream" in headers.get("accept", "").lower()
    timeout = httpx.Timeout(settings.sse_read_timeout_seconds if is_sse else settings.http_default_read_timeout_seconds)

    if is_sse:
        client_sse = httpx.AsyncClient(timeout=timeout)
        r = await _do_req(client_sse)
        async def _gen():
            try:
                async for chunk in r.aiter_bytes(): yield chunk
            finally:
                await r.aclose()
                await client_sse.aclose()
        return StreamingResponse(_gen(), status_code=r.status_code, media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=timeout) as client_http:
            r = await _do_req(client_http)
            return Response(content=r.content, status_code=r.status_code, headers={k:v for k,v in r.headers.items() if k.lower() != 'content-length'})

# --- Routes ---

@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_entry(request: Request, path: str = "mcp"):
    key = request.query_params.get("session") or request.query_params.get("workflowId") or "default"
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if not sid or sid not in SESSIONS:
            async with CREATE_SESSION_LOCK:
                # Re-check inside lock
                sid = STICKY_BY_KEY.get(key)
                if not sid or sid not in SESSIONS:
                    session_id = secrets.token_hex(8)
                    si = await _create_pod_and_service(session_id, _get_namespace())
                    if MCP_AUTO_INIT: await _ensure_mcp_initialized(si)
                    SESSIONS[session_id] = si
                    STICKY_BY_KEY[key] = session_id
                    sid = session_id
    return await proxy_any(sid, path, request)

@app.on_event("startup")
async def startup():
    _configure_logging()
    if KUBE_AVAILABLE:
        asyncio.create_task(_gc_loop())
        log.info("Garbage Collector started.")
    log.info("Ready.")

@app.get("/health")
async def health(): return {"ok": True}

