# My Kubernetes operators: Scaler and PodRestarter

I built two small Kubernetes operators in Python with [kopf](https://kopf.readthedocs.io), on a local kind cluster:

1. **Scaler**: keeps a Deployment at the replica count I ask for, and puts it back if someone changes it by hand.
2. **PodRestarter**: does a rolling restart of a Deployment every N minutes (for example `2m`, `1h30m`).

Each one has three parts:

- a **CRD** (CustomResourceDefinition), which teaches Kubernetes a new resource type
- a **CR** (custom resource), which is one object of that type, the thing I `kubectl apply`
- the **operator**, the Python program that watches those objects and does the work

---

## How it works (the reconcile loop)

Both operators follow the same loop:

1. **Read what's wanted:** the CR's `spec` (e.g. `replicas: 5`).
2. **Read what is:** the actual Deployment in the cluster.
3. **Compare** the two.
4. **Act** only if they differ: patch the Deployment.
5. **Write status:** record what I saw or did in the CR's `status`.

I made the loop **idempotent**: running it twice does nothing the second time, because step 4 only acts on a difference. That's what makes it safe for kopf to call my function as often as it likes.

**spec vs status:**

- `spec` is what's **wanted**. I write it in the CR.
- `status` is what **happened**. The operator writes it, I never do.

---

## Files

| File | What it is |
| --- | --- |
| `scaler/scaler-crd.yaml` | CRD for `Scaler` (short name `sclr`) |
| `scaler/scaler.yaml` | CR: scale Deployment `web` to 5 |
| `scaler/my_scaler.py` | The Scaler operator |
| `podrestarter/restarter-crd.yaml` | CRD for `PodRestarter` (short name `pr`) |
| `podrestarter/restarter.yaml` | CR: restart Deployment `web` every 2 minutes |
| `podrestarter/my_restarter.py` | The PodRestarter operator |
| `requirements.txt` | Python packages: kopf and the kubernetes client |

I run every command below from this folder (`kubernetes-operators/`).

---

## Setup

```bash
kind create cluster                                 # Docker Desktop must be running
kubectl get nodes                                   # one node, Ready

python3 -m venv .venv                               # a Python environment just for this
source .venv/bin/activate                           # (do this in every new terminal)
pip install -r requirements.txt                     # kopf + the kubernetes client

kubectl create deployment web --image=nginx:1.30.6-alpine --replicas=2
kubectl rollout status deploy/web
```

I pin the image tag instead of using `:latest`. `:latest` can point at a different image tomorrow, so two pods of the same Deployment could run different code.

I use two terminals: **T1** runs the operator, **T2** runs `kubectl`.

---

## Part 1: the Scaler

### 1.1 Create the CRD

```bash
kubectl apply -f scaler/scaler-crd.yaml
kubectl api-resources | grep scalers     # scalers  sclr  ops.one2n.in/v1alpha1  true  Scaler
kubectl get sclr                         # "No resources found": the type exists, no objects yet
```

What matters in the CRD:

- **`metadata.name` must be `<plural>.<group>`**, here `scalers.ops.one2n.in`.
- **The group and version** (`ops.one2n.in`, `v1alpha1`) become the `apiVersion` I use in the CR: `ops.one2n.in/v1alpha1`.
- **The schema validates the spec:** `deploymentName` is a non-empty string, and `replicas` is an integer ≥ 0. Both are required. A bad CR is rejected by the API server before it's ever saved.
- **`subresources: status: {}`** makes `status` a separate part of the object. Users editing the spec can't overwrite it, and the operator updates it through its own endpoint, `scalers/status`.
- **`status` must be a sibling of `spec`** under `properties`, not nested inside `spec`. I got the indentation wrong the first time, and status writes silently disappeared. To check the nesting:

  ```bash
  python -c "import yaml; p=yaml.safe_load(open('scaler/scaler-crd.yaml'))['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']; print(list(p), list(p['spec']['properties']))"
  # ['spec', 'status'] ['deploymentName', 'replicas']
  kubectl apply --dry-run=server -f scaler/scaler-crd.yaml    # full API-server validation, nothing saved
  ```

### 1.2 Create the CR

```bash
kubectl apply -f scaler/scaler.yaml
kubectl get scaler web-scaler -o yaml    # spec shows deploymentName: web, replicas: 5
kubectl get deploy web                   # still 2: nothing acts on a Scaler yet
```

A CR on its own does nothing. It's only data stored in the API server until an operator reads it.

**Validation check:** I set `replicas: -1` and applied again. The API server rejected it because of the `minimum: 0` in the schema. Then I set it back to 5.

### 1.3 Run the operator

```bash
# T1
kopf run scaler/my_scaler.py --standalone --verbose -A
```

- `--standalone`: run on my own, without kopf's peering (coordination between multiple copies of the operator)
- `--verbose`: show what kopf is doing
- `-A`: watch every namespace

```bash
# T2
kubectl get deploy web                   # now 5
kubectl get sclr web-scaler -o yaml      # status.currentReplicas is set
```

### 1.4 What's in `my_scaler.py`

**Login (`@kopf.on.startup`).** kopf has its own connection for watching Scalers, but the `kubernetes` client library needs its own. I try the in-cluster config first (the pod's service account) and fall back to `~/.kube/config` (kind) when it runs on my laptop.

**Four triggers on one function:**

| Trigger | When it fires |
| --- | --- |
| `on.create` | A new Scaler is applied |
| `on.update(field="spec")` | A Scaler's **spec** changes. Changes to status or labels don't trigger it, so writing my own status doesn't call me again |
| `on.resume` | The operator restarts and finds Scalers that already exist |
| `timer(interval=60)` | Every 60 s per Scaler: the drift check |

I need the timer because my triggers are all about **Scalers**. If someone runs `kubectl scale deploy web --replicas=1`, the Deployment changes and the Scaler doesn't, so no event reaches me. The timer catches the drift within 60 s.

**A missing Deployment is not a crash.** If `web` doesn't exist yet, the read returns a 404, and I raise `kopf.TemporaryError(..., delay=30)`: "not ready yet, try again in 30 s". Any other API error (such as a 403 from missing permissions) I re-raise, so it shows up in the logs.

**The patch sends only `spec.replicas`,** so I don't overwrite anything else on the Deployment.

**Status goes through `patch.status`,** not `return`. A returned value would land under `status.reconcile_scaler.…` instead of `status.currentReplicas`. The value comes from `deploy.status.replicas or 0`: that field is missing when there are 0 pods. I read it **before** my patch, so right after a scale it still shows the old count until the next timer run.

### 1.5 Tests I ran

kopf doesn't reload my code, so after every change I stopped the operator in T1 (Ctrl+C) and started it again.

**It acts on a new Scaler.** `on.create` only fires for new objects, so I re-created the Scaler while watching the Deployment:

```bash
kubectl get deploy web -w                # in one terminal
kubectl delete scaler web-scaler && kubectl apply -f scaler/scaler.yaml
# web goes from 2 to 5
```

**kopf's memory of the spec.** After the first run, `kubectl get scaler web-scaler -o yaml` shows the annotation `kopf.zalando.org/last-handled-configuration`. That's how kopf knows next time whether the spec changed. I never write it myself.

**Status is written.** After re-creating the Scaler, `kubectl get scaler web-scaler -o yaml` shows `status.currentReplicas`. The `patch.status` line has to be at the function's indent level, not inside the `if`. Inside the `if`, a Deployment that's already at the right count never gets a status.

**Edits and restarts.** When I restart T1, `on.resume` runs straight away for `web-scaler`. Then I edited the spec:

```bash
kubectl patch scaler web-scaler --type merge -p '{"spec":{"replicas":3}}'
kubectl get deploy web                   # 3 (on.update)
```

**Drift.** Before I added the timer, a manual scale stayed in place, because nothing about the Scaler changed:

```bash
kubectl scale deployment web --replicas=1
kubectl get deploy web                   # 1, and it stays 1
```

With the timer added, the same command gets undone: `kubectl get deploy web -w` goes back to 3 within 60 s.

### 1.6 Clean up before Part 2

```bash
# Ctrl+C in T1
kubectl delete scaler --all
```

---

## Part 2: the PodRestarter

### 2.1 Create the CRD

```bash
kubectl apply -f podrestarter/restarter-crd.yaml
kubectl api-resources | grep podrestarters
```

The difference from the Scaler is the `interval` field: a string like `30m`, `6h` or `1h30m`.

- `pattern: '^([0-9]+h)?([0-9]+m)?([0-9]+s)?$'`: optional hours, then minutes, then seconds, in that order. `^…$` means the whole string must match.
- `minLength: 2`: every part of the pattern is optional, so an empty string would match. This rejects it (and anything like `m` or `5`).

### 2.2 Create the CR

```bash
kubectl apply -f podrestarter/restarter.yaml          # restart web every 2m
kubectl get pr web-restarter -o yaml
```

### 2.3 Run the operator

```bash
# T1
kopf run podrestarter/my_restarter.py --standalone --verbose -A

# T2
kubectl get pods -w                      # new pods roll in roughly every 2 minutes
```

### 2.4 What's in `my_restarter.py`

**A timer, not create/update handlers.** Nothing in the PodRestarter changes when a restart is due, so create/update events never fire for it. `@kopf.timer(..., interval=30)` runs my function every 30 s for each PodRestarter.

**30 s is how often I check, not how often I restart.** Each check does this:

```python
last = status.get("lastRestartTime")                   # from status, written by me
next_due = dt.datetime.fromisoformat(last) + interval  # interval comes from spec
if now < next_due:
    return                                             # not due yet
```

With `interval: 2m` and the first restart at 10:00:00:

| Check | `now < 10:02:00`? | Result |
| --- | --- | --- |
| 10:00:30 | yes | skip |
| 10:01:00 | yes | skip |
| 10:01:30 | yes | skip |
| ~10:02:00 | no | restart, save the new time |

- **The first check restarts straight away:** there's no `lastRestartTime` yet.
- **kopf waits 30 s after the previous run ends,** so each check lands slightly later than the last, and the restart happens just after it's due.
- **An interval that isn't a multiple of 30 s** waits for the next check. A restart can be up to ~30 s late.

**`parse_interval`** turns `"1h30m"` into a `timedelta` with a regex. If the string is bad, it raises `kopf.PermanentError`: retrying won't help, the CR itself has to be fixed. I also clamp the result to `MIN_INTERVAL` (1 minute), so a `1s` interval can't restart the app constantly.

**How I restart:** I don't delete pods. I patch an annotation in the Deployment's **pod template**:

```yaml
spec:
  template:
    metadata:
      annotations:
        kubectl.kubernetes.io/restartedAt: "<now>"
```

Any change to the pod template makes the Deployment roll out new pods gradually, respecting `maxUnavailable` and `maxSurge`, so the app stays up. This is exactly what `kubectl rollout restart` does.

**Why `lastRestartTime` lives in status.** It's stored in the cluster, not in my program's memory. If the operator restarts, it reads the time back and carries on with the same schedule, instead of restarting the app again straight away. I write it **after** the patch, so a restart that failed is never recorded as done.

### 2.5 Tests I ran

**Validation.** I set `interval: 5d` in `restarter.yaml` and applied it. The API server rejected it because `d` doesn't match the pattern. Then I set it back to `2m`.

**The timer ticks.** With only the timer in place, T1 logged a `Tick` line every 30 s.

**Before status was saved, it restarted on every tick.** With the due check and the restart in place but no `lastRestartTime` write, T1 said `Restart is due` on every tick, and `kubectl get pods -w` showed new pods every 30 s. This is exactly the bug from 2.6, point 1.

**The annotation is the same as `rollout restart`'s:**

```bash
kubectl rollout restart deployment web
kubectl get deploy web -o yaml | grep restartedAt
```

**With status saved:** one restart, then `Not due yet; next restart at …` on each tick, then another restart about 2 minutes later.

```bash
kubectl get podrestarter web-restarter -o yaml     # status.lastRestartTime
```

**It survives an operator restart.** Ctrl+C in T1 and start it again straight away: it says `Not due yet`, and it doesn't restart the app again.

### 2.6 "It restarts every few seconds": what I'd check

1. **Can the operator write status?** If it can't (no RBAC permission on `podrestarters/status`), `lastRestartTime` is never saved, so every 30 s check thinks a restart is due.
2. **Is the interval tiny?** That's why `MIN_INTERVAL` exists.
3. **Are two copies of the operator running?** Then both act on their own schedules. I'd run 1 replica with `strategy: Recreate`.

### 2.7 Clean up

```bash
# Ctrl+C in T1
kubectl delete podrestarter --all
kubectl delete scaler --all
```

I keep the `web` Deployment and the CRDs for the next practice round.

---

## Notes

- **Running in the cluster instead of on my laptop.** Locally, the operator uses my kubeconfig, which has full admin access, so I didn't need any RBAC. In a pod, it would need a ServiceAccount plus a ClusterRole allowing it to:
  - watch and list its CRs
  - patch `<plural>/status`
  - get and patch `deployments`

  kopf also needs to list CRDs and namespaces, and to create events.
- **kubebuilder vs kopf.** kubebuilder is the Go tool and generates the CRD and RBAC from code markers. In kopf I write the CRD YAML myself. The ideas are the same in both (reconcile, idempotency, status, requeue).
- **A periodic restart is usually a workaround.** It's typically used for a memory leak or for config that's only read at startup. I'd rather fix the cause, or restart only when a Secret or ConfigMap actually changes (Stakater Reloader). I'd write this operator only when that isn't possible.
