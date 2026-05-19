# Lab 3

This lab adds observability to the Communication email worker.

## Changes

- `lab3-worker-observable/worker.py` keeps the Lab 2 REST API approach and adds structured JSON logs, OpenTelemetry traces, and Prometheus metrics.
- The worker exports traces to Jaeger and creates spans for processing a Communication, serializing the email body, and sending the SMTP email.
- The worker exposes Prometheus metrics on `http://localhost:8000/metrics` for processed emails, processing duration, SMTP duration, and polling results.
- `mzinga-apps-COPY/src/collections/Communications.ts` keeps Communications compatible with an external worker, so worker status updates are not reset by MZinga.

## Run On Linux

Start from the repository root, the folder that contains `README.md`, `mzinga-apps-COPY/`, and `lab3-worker-observable/`.

Set up and start MZinga infrastructure:

```sh
cd mzinga-apps-COPY
cp -n .env.example .env
npm install
mkdir -p /tmp/database /tmp/mzinga /tmp/messagebus
docker compose up -d replica_key database messagebus cache jaeger
```

Ensure `mzinga-apps-COPY/.env` has tracing enabled and the external worker flag:

```sh
DISABLE_TRACING=0
COMMUNICATIONS_EXTERNAL_WORKER=true
```

In a second terminal:

```sh
cd mzinga-apps-COPY
npm run dev
```

Start MailHog for local SMTP testing:

```sh
docker run -d --name mailhog -p 1025:1025 -p 8025:8025 mailhog/mailhog
```

## Observable Worker

Stop the Lab 1 and Lab 2 workers if they are running, then start the observable worker:

```sh
cd lab3-worker-observable
cp -n .env.example .env
python -m venv ../.venv
source ../.venv/bin/activate
pip install -r requirements.txt
python worker.py
```

Edit `lab3-worker-observable/.env` and set `MZINGA_EMAIL` and `MZINGA_PASSWORD` to the local admin user you created in MZinga.

## Check

Open `http://localhost:3000/admin`, create a user, then create a Communication for that user. The observable worker should poll the API, mark the Communication `sent`, and the email should appear in MailHog at `http://localhost:8025`.

Worker metrics are available at:

```sh
http://localhost:8000/metrics
```

Useful metric check:

```sh
curl -s http://localhost:8000/metrics | grep -E 'emails_processed|email_processing_duration|smtp_send_duration|worker_poll'
```

Traces are available in Jaeger at `http://localhost:16686`. Search with:

```text
Service: email-worker
Operation: process_communication
Lookback: 1h
```

To test the failure path, stop MailHog and create another Communication. The worker should log a failure, mark the Communication `failed`, and increase `emails_processed_total{status="failed",...}`.
