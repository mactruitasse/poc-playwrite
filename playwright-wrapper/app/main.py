import os
import uuid
import logging
from flask import Flask, request
from kubernetes import client, config, watch

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Chargement de la config K8s
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

v1 = client.CoreV1Api()
NAMESPACE = "n8n-prod"

@app.route('/mcp', methods=['POST'])
def proxy_mcp():
    session_id = request.args.get("session") or str(uuid.uuid4())[:8]
    pod_name = f"pw-worker-{session_id}"
    
    log.info(f"Création du pod worker pour la session: {session_id}")

    # Définition du Pod Worker
    pod_spec = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            labels={"app": "playwright-worker", "session": session_id}
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="playwright",
                    image="playwright-server-local:latest",
                    image_pull_policy="Never", # Obligatoire pour charger l'image locale Kind
                    ports=[client.V1ContainerPort(container_port=8933)],
                    env=[client.V1EnvVar(name="HOME", value="/tmp")]
                )
            ],
            restart_policy="Never"
        )
    )

    try:
        v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_spec)
        
        # Attente du statut 'Running'
        w = watch.Watch()
        for event in w.stream(v1.list_namespaced_pod, namespace=NAMESPACE, label_selector=f"session={session_id}"):
            if event['object'].status.phase == 'Running':
                pod_ip = event['object'].status.pod_ip
                log.info(f"Pod {pod_name} prêt sur l'IP {pod_ip}")
                w.stop()
                return {
                    "status": "Ready",
                    "mcp_url": f"http://{pod_ip}:8933",
                    "session_id": session_id
                }
    except Exception as e:
        log.error(f"Erreur K8s: {str(e)}")
        return {"error": str(e)}, 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
