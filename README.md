# Lab 4

This lab practises Kubernetes deployment strategies with a small containerised HTTP service.

The demo service is independent from the MZinga application. It exists only to make deployment behaviour easy to observe: each response shows the application version, colour, and the Pod hostname that handled the request.

## Changes

- `lab4-k8s/app.py` implements a minimal Python HTTP server using only the standard library.
- `lab4-k8s/Dockerfile` builds the same app into two image variants by passing `APP_VERSION` and `APP_COLOR` as build arguments.
- `lab4-k8s/k8s/namespace.yaml` creates the `mzinga-lab4` namespace used by all Kubernetes resources.
- `lab4-k8s/k8s/rolling/` contains the manifests for an in-place rolling update from v1 to v2.
- `lab4-k8s/k8s/recreate/` contains the manifests for a Recreate deployment, where all old Pods stop before new Pods start.
- `lab4-k8s/k8s/blue-green/` contains two parallel deployments, blue and green, with traffic switched by patching the Service selector.
- `lab4-k8s/k8s/canary/` contains stable and canary deployments sharing one Service, so traffic is split by replica count.

## Run On Linux

Start from the repository root.

Verify Docker, Minikube, and kubectl:

```sh
docker version
minikube status
kubectl get nodes
```

If Minikube is not running:

```sh
minikube start --driver=docker --cpus=2 --memory=4096
```

Create the namespace:

```sh
cd lab4-k8s
kubectl apply -f k8s/namespace.yaml
```

## Build Images

Build the two local images:

```sh
docker build --build-arg APP_VERSION=1.0.0 --build-arg APP_COLOR=blue -t mzinga-webapp:1.0.0 .
docker build --build-arg APP_VERSION=2.0.0 --build-arg APP_COLOR=green -t mzinga-webapp:2.0.0 .
```

Load them into Minikube:

```sh
minikube image load mzinga-webapp:1.0.0
minikube image load mzinga-webapp:2.0.0
```

Check:

```sh
minikube image ls | grep mzinga-webapp
```

## Rolling Update

Deploy v1:

```sh
kubectl apply -f k8s/rolling/service.yaml
kubectl apply -f k8s/rolling/deployment-v1.yaml
kubectl rollout status deployment/webapp -n mzinga-lab4
```

Port-forward the Service:

```sh
kubectl port-forward service/webapp 8080:80 -n mzinga-lab4
```

In another terminal:

```sh
curl -s http://localhost:8080/
```

Trigger the rolling update to v2:

```sh
kubectl apply -f k8s/rolling/deployment-v2.yaml
kubectl get pods -n mzinga-lab4 -w
```

Rollback:

```sh
kubectl rollout undo deployment/webapp -n mzinga-lab4
kubectl rollout status deployment/webapp -n mzinga-lab4
```

Clean up before the next strategy:

```sh
kubectl delete -f k8s/rolling/deployment-v1.yaml
kubectl delete -f k8s/rolling/service.yaml
```

## Recreate

Deploy v1 with the Recreate strategy:

```sh
kubectl apply -f k8s/recreate/service.yaml
kubectl apply -f k8s/recreate/deployment-v1.yaml
kubectl rollout status deployment/webapp -n mzinga-lab4
```

Start the same port-forward and request loop used above. Then trigger v2:

```sh
kubectl apply -f k8s/recreate/deployment-v2.yaml
kubectl get pods -n mzinga-lab4 -w
```

During this transition the Service has a short downtime window because Kubernetes terminates all v1 Pods before starting v2 Pods.

Rollback:

```sh
kubectl rollout undo deployment/webapp -n mzinga-lab4
kubectl rollout status deployment/webapp -n mzinga-lab4
```

Clean up:

```sh
kubectl delete -f k8s/recreate/deployment-v2.yaml
kubectl delete -f k8s/recreate/service.yaml
```

## Blue-Green

Deploy both environments:

```sh
kubectl apply -f k8s/blue-green/blue-deployment.yaml
kubectl apply -f k8s/blue-green/green-deployment.yaml
kubectl apply -f k8s/blue-green/service.yaml
kubectl rollout status deployment/webapp-blue -n mzinga-lab4
kubectl rollout status deployment/webapp-green -n mzinga-lab4
```

The Service initially points to blue:

```sh
kubectl port-forward service/webapp 8080:80 -n mzinga-lab4
curl -s http://localhost:8080/
```

Test green directly:

```sh
GREEN_POD=$(kubectl get pods -n mzinga-lab4 -l slot=green -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward pod/$GREEN_POD 8081:8080 -n mzinga-lab4
curl -s http://localhost:8081/
```

Switch traffic to green:

```sh
kubectl patch service webapp -n mzinga-lab4 -p '{"spec":{"selector":{"app":"webapp","slot":"green"}}}'
curl -s http://localhost:8080/
```

Rollback instantly to blue:

```sh
kubectl patch service webapp -n mzinga-lab4 -p '{"spec":{"selector":{"app":"webapp","slot":"blue"}}}'
curl -s http://localhost:8080/
```

Clean up:

```sh
kubectl delete -f k8s/blue-green/
```

## Canary

Deploy stable and canary:

```sh
kubectl apply -f k8s/canary/service.yaml
kubectl apply -f k8s/canary/stable-deployment.yaml
kubectl apply -f k8s/canary/canary-deployment.yaml
kubectl rollout status deployment/webapp-stable -n mzinga-lab4
kubectl rollout status deployment/webapp-canary -n mzinga-lab4
```

The initial split is 9 stable Pods and 1 canary Pod.

Check from inside the cluster, because `kubectl port-forward service/webapp` can stick to one backend Pod and hide the traffic split:

```sh
STABLE_POD=$(kubectl get pod -n mzinga-lab4 -l track=stable -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n mzinga-lab4 "$STABLE_POD" -- python -c 'import json, urllib.request, collections; c=collections.Counter(); [c.update([json.load(urllib.request.urlopen("http://webapp/"))["version"]]) for _ in range(100)]; print(c)'
```

Increase canary to about 30 percent:

```sh
kubectl scale deployment/webapp-stable --replicas=7 -n mzinga-lab4
kubectl scale deployment/webapp-canary --replicas=3 -n mzinga-lab4
```

Increase canary to about 50 percent:

```sh
kubectl scale deployment/webapp-stable --replicas=5 -n mzinga-lab4
kubectl scale deployment/webapp-canary --replicas=5 -n mzinga-lab4
```

Promote v2 fully:

```sh
kubectl scale deployment/webapp-stable --replicas=0 -n mzinga-lab4
kubectl scale deployment/webapp-canary --replicas=10 -n mzinga-lab4
```

Abort the canary and return to v1:

```sh
kubectl scale deployment/webapp-canary --replicas=0 -n mzinga-lab4
kubectl scale deployment/webapp-stable --replicas=10 -n mzinga-lab4
```

## Check

Useful checks while running the lab:

```sh
kubectl get pods -n mzinga-lab4
kubectl get deployments -n mzinga-lab4
kubectl get service webapp -n mzinga-lab4 -o yaml
kubectl get endpoints webapp -n mzinga-lab4
```

Expected strategy behaviour:

- Rolling Update shows v1 and v2 live at the same time during the rollout.
- Recreate shows a downtime gap between v1 termination and v2 readiness.
- Blue-Green switches traffic by changing `slot: blue` to `slot: green` on the Service selector.
- Canary shifts traffic by changing stable and canary replica counts.

## Cleanup

Delete every Lab 4 resource:

```sh
kubectl delete namespace mzinga-lab4
```

Stop Minikube if you are done:

```sh
minikube stop
```

