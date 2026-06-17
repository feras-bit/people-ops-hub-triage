# People & Ops Hub — Triage function (deploy)

Does what Asana paid "Rules" would do, via the Asana API. Same stack as
`hr-reports-gcp` / `personio-quinyx-sync` (Python, functions-framework, Frankfurt).

## 1. Get an Asana Personal Access Token
Asana → Settings → Apps → **Personal access tokens** → Create. Copy it.
(The token must belong to a user who can edit the People & Ops Hub project.)

## 2. Store secrets
```bash
PROJECT=hr-project-492008
REGION=europe-west3

printf '%s' 'YOUR_ASANA_PAT' | gcloud secrets create ASANA_PAT \
  --data-file=- --project=$PROJECT       # or: versions add ASANA_PAT --data-file=-
```
(Slack is optional — reuse the SLACK_BOT_TOKEN you already have for hr-reports.)

## 3. Deploy (HTTP, Gen2)
```bash
gcloud functions deploy people-ops-triage \
  --gen2 --runtime=python312 --region=$REGION \
  --source=. --entry-point=triage --trigger-http --no-allow-unauthenticated \
  --set-secrets=ASANA_PAT=ASANA_PAT:latest \
  --set-env-vars=SLACK_CHANNEL_ID=YOUR_CHANNEL_ID \
  --set-secrets=SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest \
  --project=$PROJECT
```
Add `--set-env-vars=SEC_JOB_ORG=<section_gid>` once the 🏢 Job & Org Change lane
exists (until then those tickets just stay in New (Untriaged)).

## 4. Trigger it — pick ONE

### A) Cloud Scheduler (simplest, every 15 min)
```bash
URL=$(gcloud functions describe people-ops-triage --gen2 --region=$REGION \
      --project=$PROJECT --format='value(serviceConfig.uri)')

gcloud scheduler jobs create http people-ops-triage-15m \
  --schedule="*/15 * * * *" --uri="$URL" --http-method=POST \
  --oidc-service-account-email=YOUR_RUNTIME_SA --location=$REGION --project=$PROJECT
```

### B) Asana webhook (near real-time)
Register a webhook so each board change calls the function. The function already
answers the X-Hook-Secret handshake.
```bash
URL=...  # the function URL from above (must allow the handshake request through)
curl -sX POST https://app.asana.com/api/1.0/webhooks \
  -H "Authorization: Bearer YOUR_ASANA_PAT" -H "Content-Type: application/json" \
  -d '{"data":{"resource":"1215627193057064","target":"'"$URL"'"}}'
```
Cron (A) is recommended to start — it's idempotent and dead simple. Add the
webhook later if you want instant routing.

## Safety notes
- The sweep is **idempotent** and only fills blanks (assignee / status / due
  date) — it never overwrites a value a human set, so re-running is safe.
- `❓ Other / Not sure` tickets are left in New (Untriaged) on purpose.
- For a production webhook you should also verify the `X-Hook-Signature` HMAC
  (using the secret from the handshake) before acting — add that if you expose
  the function publicly.
