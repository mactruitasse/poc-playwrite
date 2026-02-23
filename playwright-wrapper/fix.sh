#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Patch app/main.py (rewrite full file) to:
# - Stream SSE only for GET requests (avoid n8n hanging on POST)
# - For non-GET MCP calls, force Accept: application/json
# - If upstream still returns text/event-stream on non-GET, convert first SSE data: line to JSON and close.
# -----------------------------------------------------------------------------

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
TARGET="$REPO_ROOT/app/main.py"

if [[ ! -f "$TARGET" ]]; then
  echo "ERROR: $TARGET not found. Run from the repo root (the directory that contains ./app/main.py)."
  exit 1
fi

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET.bak.$TS"

echo "[+] Repo root : $REPO_ROOT"
echo "[+] Target    : $TARGET"
echo "[+] Backup    : $BACKUP"

cp -a "$TARGET" "$BACKUP"

cat > "$TARGET" <<'PY'
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

log = logging.getLogger(__name__)
logger = log  # alias for debug patches

app = FastAPI(title="Playwright Wrapper (Transparent Proxy)", version="1.0.0")


@app.get("/health")
async def health():
    return {"ok": True}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _split_args(s: Optional[str]) -> Optional[List[str]]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
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


def _fail_fast_token():
    # Auth disabled in this code path (no token validation).
    # If you re-enable auth, do it here.
    return


def _configure_logging():
    level = (settings.log_level or "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@dataclass
class SessionInfo:
    session_id: str
    namespace: str
    created_at: float = field(default_factory=lambda: time.time())
    pod_name: Optional[str] = None
    service_name: Optional[str] = None
    target_url: Optional[str] = None  # e.g. http://svc:8933
    last_access: float = field(default_factory=lambda: time.time())
    # MCP initialize returns an mcp-session-id header which must be reused.
    mcp_session_id: Optional[str] = None


SESSIONS: Dict[str, SessionInfo] = {}
STICKY_BY_KEY: Dict[str, str] = {}
STICKY_LOCK = asyncio.Lock()
MCP_INIT_LOCKS: Dict[str, asyncio.Lock] = {}

KUBE_AVAILABLE = _load_kube()
COREV1 = client.CoreV1Api() if KUBE_AVAILABLE else None

KEEP_FAILED_RESOURCES = _env_bool("KEEP_FAILED_RESOURCES", False)

UPSTREAM_HOST_HEADER = os.getenv("UPSTREAM_HOST_HEADER", "localhost")
MCP_AUTO_INIT = _env_bool("MCP_AUTO_INIT", True)


def _get_namespace() -> str:
    if settings.target_namespace:
        return settings.target_namespace
    ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    if os.path.exists(ns_path):
        try:
            return open(ns_path, "r", encoding="utf-8").read().strip()
        except Exception:
            pass
    return "default"


def _httpx_timeouts(read_timeout: float) -> httpx.Timeout:
    try:
        rt_val = float(read_timeout)
    except Exception:
        rt_val = 0.0
    rt = None if rt_val <= 0.0 else rt_val
    return httpx.Timeout(
        connect=settings.http_connect_timeout_seconds,
        read=rt,
        write=settings.sse_write_timeout_seconds,
        pool=settings.http_connect_timeout_seconds,
    )


def _is_mcp_path(path: str) -> bool:
    p = (path or "").lstrip("/")
    return p == "mcp" or p.startswith("mcp/")


def _sticky_key_from_request(request: Request) -> str:
    """
    IMPORTANT:
    - n8n may hit GET /mcp multiple times.
    - If we always use workflowId/default, we reuse the same upstream MCP session,
      which leads to upstream 409 conflicts (observed in logs).
    - Therefore we honor ?session=... if provided, fallback to workflowId, else default.
    """
    return request.query_params.get("session") or request.query_params.get("workflowId") or "default"


async def _ensure_mcp_initialized(si: SessionInfo) -> None:
    if si.mcp_session_id:
        return
    lock = MCP_INIT_LOCKS.setdefault(si.session_id, asyncio.Lock())
    async with lock:
        if si.mcp_session_id:
            return
        if not si.target_url:
            raise HTTPException(status_code=404, detail="Unknown session")

        upstream = f"{si.target_url.rstrip('/')}/mcp"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "host": UPSTREAM_HOST_HEADER,
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "playwright-wrapper", "version": app.version},
                "capabilities": {},
            },
        }

        timeout = _httpx_timeouts(settings.http_default_read_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client_http:
            r = await client_http.post(upstream, headers=headers, json=payload)
            sid = r.headers.get("mcp-session-id")
            if not sid:
                body = (r.text or "")[:500]
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP initialize did not return mcp-session-id (status={r.status_code}): {body}",
                )
            si.mcp_session_id = sid.strip()


async def _tcp_wait(host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(1.0)
    raise TimeoutError(f"TCP connect to {host}:{port} timed out after {timeout}s: {last_err!s}")


def _playwright_container_spec() -> client.V1Container:
    cmd = (settings.playwright_command or "").strip() or None
    args = _split_args(settings.playwright_args)

    if cmd in ("sh", "/bin/sh") and args:
        if args[0] in ("-c", "-lc") and len(args) > 2:
            toks: List[str] = []
            for t in args[1:]:
                if t in ("&&", "'&&'", '"&&"'):
                    toks.append("&&")
                elif t in ("*", "'*'", '"*"'):
                    toks.append("*")
                else:
                    toks.append(t)
            shell_cmd = " ".join(toks).strip()
            args = [args[0], shell_cmd]

    return client.V1Container(
        name="playwright",
        image=settings.playwright_image,
        image_pull_policy="IfNotPresent",
        command=[cmd] if cmd else None,
        args=args,
        ports=[client.V1ContainerPort(container_port=settings.playwright_port)],
        security_context=None,
        volume_mounts=None,
    )


def _build_security_contexts():
    container_security_ctx = None
    pod_security_ctx = None

    if settings.run_as_non_root:
        container_security_ctx = client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=settings.run_as_user,
            run_as_group=settings.run_as_group,
            allow_privilege_escalation=settings.allow_privilege_escalation,
            read_only_root_filesystem=settings.read_only_root_filesystem,
        )

        if settings.drop_all_caps:
            container_security_ctx.capabilities = client.V1Capabilities(drop=["ALL"])

    if settings.fs_group:
        pod_security_ctx = client.V1PodSecurityContext(fs_group=settings.fs_group)

    if settings.seccomp_runtime_default:
        if container_security_ctx is None:
            container_security_ctx = client.V1SecurityContext()
        container_security_ctx.seccomp_profile = client.V1SeccompProfile(type="RuntimeDefault")

    return container_security_ctx, pod_security_ctx


def _pvc_volumes():
    if not settings.enable_pvc_mount:
        return None, None
    vol = client.V1Volume(
        name="artifacts",
        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=settings.pvc_name),
    )
    vm = client.V1VolumeMount(name="artifacts", mount_path=settings.pvc_mount_path)
    return [vol], [vm]


def _svc_type() -> str:
    v = (settings.service_type or "ClusterIP").strip()
    if v not in ("ClusterIP", "NodePort"):
        raise HTTPException(status_code=500, detail=f"Unsupported SERVICE_TYPE: {v}")
    return v


def _session_expired(si: SessionInfo) -> bool:
    ttl = float(settings.session_ttl_minutes) * 60.0
    return (time.time() - si.last_access) > ttl


async def _gc_loop():
    while True:
        try:
            await asyncio.sleep(settings.gc_interval_seconds)
            expired = [sid for sid, si in list(SESSIONS.items()) if _session_expired(si)]
            for sid in expired:
                try:
                    await delete_session(sid)
                except Exception as e:
                    log.warning("GC delete failed for %s: %s", sid, e)
        except Exception as e:
            log.warning("GC loop error: %s", e)


@app.on_event("startup")
async def startup():
    _configure_logging()
    _fail_fast_token()
    asyncio.create_task(_gc_loop())


async def _create_pod_and_service(session_id: str, namespace: str) -> SessionInfo:
    assert COREV1 is not None

    pod_name = f"pw-{session_id}"
    svc_name = f"pw-{session_id}"

    container_security_ctx, pod_security_ctx = _build_security_contexts()

    c = _playwright_container_spec()
    c.security_context = container_security_ctx

    vols, vms = _pvc_volumes()
    if vms:
        c.volume_mounts = vms

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, labels={"app": "pw", "sid": session_id}),
        spec=client.V1PodSpec(
            containers=[c],
            restart_policy="Never",
            security_context=pod_security_ctx,
            service_account_name=None,
            volumes=vols,
        ),
    )

    try:
        COREV1.create_namespaced_pod(namespace=namespace, body=pod)
        log.info("Created pod %s/%s", namespace, pod_name)
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"K8s error creating pod: {e.reason}")

    svc_ports = [client.V1ServicePort(name="http", port=settings.playwright_port, target_port=settings.playwright_port)]
    svc_type = _svc_type()
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=svc_name, labels={"app": "pw", "sid": session_id}),
        spec=client.V1ServiceSpec(
            selector={"app": "pw", "sid": session_id},
            ports=svc_ports,
            type=svc_type,
        ),
    )

    try:
        COREV1.create_namespaced_service(namespace=namespace, body=svc)
        log.info("Created service %s/%s type=%s", namespace, svc_name, svc_type)
    except ApiException as e:
        if not KEEP_FAILED_RESOURCES:
            try:
                COREV1.delete_namespaced_pod(name=pod_name, namespace=namespace)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"K8s error creating service: {e.reason}")

    target_url = f"http://{svc_name}.{namespace}.svc:{settings.playwright_port}"
    si = SessionInfo(
        session_id=session_id,
        namespace=namespace,
        pod_name=pod_name,
        service_name=svc_name,
        target_url=target_url,
    )

    try:
        await _tcp_wait(
            host=f"{svc_name}.{namespace}.svc",
            port=settings.playwright_port,
            timeout=settings.pod_ready_timeout_seconds,
        )
    except Exception as e:
        if not KEEP_FAILED_RESOURCES:
            try:
                COREV1.delete_namespaced_service(name=svc_name, namespace=namespace)
            except Exception:
                pass
            try:
                COREV1.delete_namespaced_pod(name=pod_name, namespace=namespace)
            except Exception:
                pass
        raise HTTPException(status_code=504, detail=f"Pod/service not ready: {e!s}")

    return si


@app.post("/sessions")
async def create_session():
    if not KUBE_AVAILABLE or COREV1 is None:
        raise HTTPException(status_code=500, detail="Kubernetes API not available")

    namespace = _get_namespace()
    session_id = secrets.token_hex(8)

    si = await _create_pod_and_service(session_id=session_id, namespace=namespace)

    if MCP_AUTO_INIT:
        try:
            await _ensure_mcp_initialized(si)
        except HTTPException:
            if not KEEP_FAILED_RESOURCES:
                try:
                    await delete_session(session_id)
                except
