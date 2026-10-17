# Operator entry points. Every target is a few plain commands, so each can be
# run by hand too.
SHA   ?= $(shell git rev-parse HEAD)
IMAGE ?= ghcr.io/lokendra005/calculator
NS    ?= calculator
URL   ?= http://localhost:30080
HELM_FLAGS = --namespace $(NS) --create-namespace --set image.tag=$(SHA) \
             --set monitoring.enabled=true --wait --rollback-on-failure --timeout 3m

.PHONY: test cluster monitoring deploy build-local status smoke rollback grafana prometheus down

test:                       ## protocol + operations tests against a local process
	python3 server.py 8099 & pid=$$!; sleep 1; python3 test_client.py 8099; rc=$$?; kill $$pid; exit $$rc

cluster:                    ## local kind cluster, NodePort 30080 mapped to localhost
	kind create cluster --config deploy/kind.yaml

monitoring:                 ## Prometheus, Grafana, kube-state-metrics
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version 92.10.1 \
	  --namespace monitoring --create-namespace -f deploy/monitoring.yaml --wait --timeout 10m

deploy:                     ## release the CI-built image for SHA (default: HEAD)
	docker pull $(IMAGE):$(SHA)
	kind load docker-image $(IMAGE):$(SHA) --name calc
	helm upgrade --install calculator chart/calculator $(HELM_FLAGS)
	./deploy/smoke.sh $(URL) $(SHA)

build-local:                ## dev only: build HEAD locally instead of pulling from CI
	docker build --build-arg VERSION=$(SHA) -t $(IMAGE):$(SHA) .
	kind load docker-image $(IMAGE):$(SHA) --name calc
	helm upgrade --install calculator chart/calculator $(HELM_FLAGS)
	./deploy/smoke.sh $(URL) $(SHA)

status:                     ## which commit is serving, and the release history
	@curl -fsS $(URL)/version; echo
	@kubectl -n $(NS) get deploy calculator -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
	@helm -n $(NS) history calculator --max 5

smoke:
	./deploy/smoke.sh $(URL)

rollback:                   ## back to the previous Helm revision, then verify
	helm -n $(NS) rollback calculator --wait
	./deploy/smoke.sh $(URL)

grafana:                    ## http://localhost:3000, user admin
	@kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
	kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80

prometheus:                 ## http://localhost:9090
	kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090

down:
	kind delete cluster --name calc
