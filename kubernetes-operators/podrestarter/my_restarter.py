import kopf
import datetime as dt
import re
from kubernetes import client, config

INTERVAL_FORMAT = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
MIN_INTERVAL = dt.timedelta(minutes=1)

@kopf.on.startup()
def login(**kwargs):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

def parse_interval(text):
    match = INTERVAL_FORMAT.match(text)
    if not match or not any(match.groups()):
        raise kopf.PermanentError(f"Bad interval {text!r}. Use something like 30m, 6h or 1h30m")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)

@kopf.timer("ops.one2n.in", "v1alpha1", "podrestarters", interval=30, id="podrestarter-timer")
def check_restart(spec, status, patch, name, namespace, logger, **kwargs):
    deploy_name = spec["deploymentName"]
    interval = max(parse_interval(spec["interval"]), MIN_INTERVAL)
    now = dt.datetime.now(dt.timezone.utc)

    last = status.get("lastRestartTime")
    if last:
        next_due = dt.datetime.fromisoformat(last) + interval
        if now < next_due:
            logger.info(f"Not due yet; next restart at {next_due.isoformat()}")
            return

    stamp = now.isoformat()
    body = {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": stamp
    }}}}}
    client.AppsV1Api().patch_namespaced_deployment(name=deploy_name, namespace=namespace, body=body)
    logger.info(f"Rolling restart triggered for {namespace}/{deploy_name}")
    patch.status["lastRestartTime"] = stamp

