IMG="n8n-playwright-local:2.2.4-pw1.49.0-fix2"
POD="pw-smoke-$(date +%s)"

kubectl -n n8n-prod run "$POD" --restart=Never --image="$IMG" --command -- sh -lc '
set -eux
node -e "
const { chromium } = require(\"playwright\");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(\"https://example.com\", { waitUntil: \"domcontentloaded\" });
  console.log(\"TITLE=\", await page.title());
  await page.screenshot({ path: \"/tmp/pw.png\", fullPage: true });
  await browser.close();
})();
"
ls -la /tmp/pw.png
echo OK
'

kubectl -n n8n-prod wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" --timeout=180s
kubectl -n n8n-prod logs "$POD" --tail=200
kubectl -n n8n-prod delete pod "$POD" --wait=false
