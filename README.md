# Lab 2

This lab moves the Communication email worker further away from direct database coupling.

## Changes

- `lab2-worker-rest/worker.py` replaces direct MongoDB access with MZinga REST API calls.
- The REST worker authenticates with `/api/users/login`, polls pending Communications through `/api/communications?depth=1`, sends email through SMTP, and writes `processing`, `sent`, or `failed` back through PATCH requests.
- `lab2-worker-events/worker.py` adds the optional event-driven version using RabbitMQ and MZinga REST API calls.
- `mzinga-apps-COPY/src/collections/Communications.ts` allows authenticated admin updates and only marks newly created Communications as `pending`, so worker status updates are not reset.
- `mzinga-apps-COPY/.env.example` includes the RabbitMQ hook variables needed for the event-driven worker.

## Run On Linux

Start from the repository root, the folder that contains `README.md`, `mzinga-apps-COPY/`, `lab2-worker-rest/`, and `lab2-worker-events/`.

Set up and start MZinga:

```sh
cd mzinga-apps-COPY
cp -n .env.example .env
npm install
mkdir -p /tmp/database /tmp/mzinga /tmp/messagebus
docker compose up database messagebus cache
```

In a second terminal:

```sh
cd mzinga-apps-COPY
npm run dev
```

Start MailHog for local SMTP testing:

```sh
docker run -d -p 1025:1025 -p 8025:8025 mailhog/mailhog
```

## REST Worker

Stop the Lab 1 worker if it is running, then start the REST worker:

```sh
cd lab2-worker-rest
cp -n .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python worker.py
```

Edit `lab2-worker-rest/.env` and set `MZINGA_EMAIL` and `MZINGA_PASSWORD` to the local admin user you created in MZinga.

## Event Worker

For the event-driven worker, keep the MZinga `.env` values from `mzinga-apps-COPY/.env.example`:

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
HOOKSURL_COMMUNICATIONS_AFTERCHANGE=rabbitmq
```

Restart MZinga after changing `.env`, stop the REST worker, then run:

```sh
cd lab2-worker-events
cp -n .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python worker.py
```

Edit `lab2-worker-events/.env` and set `MZINGA_EMAIL` and `MZINGA_PASSWORD` to the same local admin user.

## Check

Open `http://localhost:3000/admin`, create a user, then create a Communication for that user. With the REST worker, the worker should poll the API and mark the Communication `sent`. With the event worker, the worker should receive the RabbitMQ event immediately and mark the Communication `sent`. If MailHog is running, the email should appear at `http://localhost:8025`.

## Observable Worker

For Lab 3, stop the Lab 1 and Lab 2 workers if they are running. Only the observable worker should process Communications.

Keep MZinga, Docker services, MailHog, and Jaeger running, then start the observable worker:

```sh
cd lab3-worker-observable
cp -n .env.example .env
source ../.venv/bin/activate
pip install -r requirements.txt
python worker.py
```

Edit `lab3-worker-observable/.env` and set `MZINGA_EMAIL` and `MZINGA_PASSWORD` to the same local admin user used in MZinga.

## Observable Worker Check

Open `http://localhost:3000/admin` and create a Communication. The worker should mark it `sent`, the email should appear in MailHog at `http://localhost:8025`, traces should appear in Jaeger at `http://localhost:16686` under service `email-worker`, and worker metrics should be available at `http://localhost:8000/metrics`.
