import kopf
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

@kopf.on.startup()
def login(**kwargs):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

@kopf.on.create("ops.one2n.in", "v1alpha1", "scalers", id="scaler-create")
@kopf.on.update("ops.one2n.in", "v1alpha1", "scalers", field="spec", id="scaler-update")
@kopf.on.resume("ops.one2n.in", "v1alpha1", "scalers", id="scaler-resume")
@kopf.timer("ops.one2n.in", "v1alpha1", "scalers", interval=60, id="scaler-drift")
def reconcile_scaler(spec, name, namespace, logger, patch, **kwargs):

    # Define the deployment name and desired replicas
    deployment_name = spec['deploymentName']
    desired_replicas = spec['replicas']
    
    # Create a Kubernetes API client
    app = client.AppsV1Api()
    # deploy = app.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    try:
        deploy = app.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise kopf.TemporaryError(f"Deployment {namespace}/{deployment_name} not found. Will retry.", delay=30)
        raise

    current_replicas = deploy.spec.replicas
    # logger.info(f"Deployment {deployment_name} currently has {current_replicas} replicas, Scaler wants {desired_replicas}")

    if current_replicas != desired_replicas:
        # Update the deployment with the desired number of replicas
        app.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body={"spec": {"replicas": desired_replicas}},
        )
        logger.info(f"Scaled {namespace}/{deployment_name} from {current_replicas} to {desired_replicas}")

    patch.status["currentReplicas"] = deploy.status.replicas or 0