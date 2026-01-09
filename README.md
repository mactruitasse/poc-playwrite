# n8n GitOps (Argo CD + Helm) - Bootstrap

This repository contains the minimum files to push to GitHub to start an Argo CD + Helm deployment.

## What is included
- apps/n8n/values-prod.yaml: example values (queue mode + Playwright community node)
- apps/n8n/application.yaml: Argo CD Application (points to this repo)
- scripts/vendor_n8n_chart.sh: helper to download and vendor the n8n chart into charts/n8n/

## IMPORTANT: vendor the chart
This bootstrap expects the Helm chart to be present in charts/n8n/.
Run:

  ./scripts/vendor_n8n_chart.sh

Then commit and push the generated charts/n8n/ directory.

## Apply the Argo CD Application

  kubectl create namespace n8n-prod
  kubectl apply -f apps/n8n/application.yaml

