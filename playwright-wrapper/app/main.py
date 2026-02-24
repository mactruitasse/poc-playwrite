import os
import uuid
import logging
import time
from flask import Flask, request
from kubernetes import client, config, watch

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Chargement de la config Kubernetes (In-Cluster ou Local)
try:
    config.load_incluster_config()
    log.info("Configurée pour s'exécuter à l'intérieur du cluster K8s.")
except Exception:
    config.load_kube_config()
    log.info("Configurée pour s'exécuter localement (kubeconfig).")

v1 = client.CoreV1Api()
NAMESPACE = os.getenv("TARGET_NAMESPACE", "n8n-prod")

@app.route('/health', methods=['GET'])
def health():
    """Endpoint pour les Probes Kubernetes."""
    return {"status": "ok"}, 200

@app.route('/mcp', methods=['POST'])
def create_mcp_worker():
    """Crée un Pod worker Playwright et attend qu'il soit prêt."""
    session_id = request.args.get("session") or str(uuid.uuid4())[:8]
    pod_name = f"pw-worker-{session_id}"
    
    log.info(f"Demande de création du pod : {pod_name}")

    # Définition du Pod
    pod_spec = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            labels={
                "app": "playwright-worker", 
                "session": session_id,
                "created-by": "playwright-wrapper"
            }
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="playwright",
                    image="playwright-server-local:latest",
                    image_pull_policy="Never",  # Important pour Kind
                    ports=[client.V1ContainerPort(container_port=8933)],
                    env=[client.V1EnvVar(name="HOME", value="/tmp")]
                )
            ],
            restart_policy="Never"
        )
    )

    try:
        # 1. Nettoyage préventif si le pod existe déjà
        try:
            v1.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
            time.sleep(1) # Petit temps pour laisser K8s purger l'ancien objet
        except:
            pass

        # 2. Création du Pod
        v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_spec)
        
        # 3. Watcher pour attendre l'état 'Running'
        w = watch.Watch()
        for event in w.stream(v1.list_namespaced_pod, namespace=NAMESPACE, 
                             label_selector=f"session={session_id}", timeout_seconds=30):
            pod = event['object']
            if pod.status.phase == 'Running' and pod.status.pod_ip:
                pod_ip = pod.status.pod_ip
                log.info(f"Pod {pod_name} est prêt ! IP: {pod_ip}")
                w.stop()
                return {
                    "status": "Ready",
                    "mcp_url": f"http://{pod_ip}:8933",
                    "session_id": session_id,
                    "pod_name": pod_name
                }, 200
        
        w.stop()
        return {"error": "Timeout : Le pod n'est pas passé en 'Running' à temps"}, 504

    except Exception as e:
        log.error(f"Erreur lors de la gestion du Pod : {str(e)}")
        return {"error": str(e)}, 500

@app.route('/mcp/stop', methods=['DELETE'])
def stop_mcp_worker():
    """Supprime un worker de manière asynchrone."""
    session_id = request.args.get("session")
    if not session_id:
        return {"error": "Paramètre 'session' manquant"}, 400
    
    pod_name = f"pw-worker-{session_id}"
    log.info(f"Demande de suppression du pod : {pod_name}")
    
    try:
        # Suppression asynchrone (Background) pour éviter de bloquer le client
        v1.delete_namespaced_pod(
            name=pod_name, 
            namespace=NAMESPACE,
            body=client.V1DeleteOptions(
                propagation_policy='Background',
                grace_period_seconds=5
            )
        )
        return {
            "status": "Deletion initiated", 
            "session_id": session_id,
            "pod_name": pod_name
        }, 200
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return {"status": "Already deleted", "session_id": session_id}, 200
        return {"error": str(e)}, 500

if __name__ == '__main__':
    # Flask tourne sur le port 8080
    app.run(host='0.0.0.0', port=8080)
