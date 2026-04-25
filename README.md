# Lab 1

This lab moves Communication email sending out of MZinga and into a Python worker.

## Changes

- `mzinga-apps-COPY/src/collections/Communications.ts` now has a read-only `status` field shown in the admin sidebar and list view.
- When `COMMUNICATIONS_EXTERNAL_WORKER=true`, MZinga saves a Communication, marks it as `pending`, and skips the old in-process email sending path.
- When the flag is false or unset, the original MZinga email sending behavior is still available.
- `lab1-worker/worker.py` polls MongoDB for `pending` communications, marks one as `processing`, resolves recipients from `users`, serializes the rich-text body to HTML, sends through SMTP, then writes `sent` or `failed`.

## Run on Linux

Install Node.js, npm, Python, Docker, and Docker Compose first. On Linux, make sure Docker is running and that your user can access it:

```sh
sudo systemctl enable --now docker
sudo groupadd docker 2>/dev/null || true
sudo usermod -aG docker "$USER"
newgrp docker
docker ps
```

If `docker ps` still says permission denied, use `sudo docker ...` for the Docker commands below, or log out and back in.

Run the following commands from the repository root, the folder that contains `README.md`, `mzinga-apps-COPY/`, and `lab1-worker/`.

Set up the MZinga environment:

```sh
cd mzinga-apps-COPY
cp .env.example .env
npm install
```

Terminal 1, start MongoDB, RabbitMQ, and Redis:

```sh
cd mzinga-apps-COPY
mkdir -p /tmp/database /tmp/mzinga /tmp/messagebus
docker compose up database messagebus cache
```

If Docker still requires root on your PC, run:

```sh
sudo docker compose up database messagebus cache
```

Terminal 2, start MZinga:

```sh
cd mzinga-apps-COPY
npm run dev
```

Terminal 3, start MailHog for local SMTP testing:

```sh
docker run -d -p 1025:1025 -p 8025:8025 mailhog/mailhog
```

Use `sudo docker run ...` if your Docker setup needs sudo.

Terminal 4, start the worker:

```sh
cd lab1-worker
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python worker.py
```

The warning about `PAYLOAD_PUBLIC_DISABLED_ENTITIES_SLUGS` is harmless. The `.env.example` file sets it to an empty value so the warning should not appear after copying it.

## Check

Open `http://localhost:3000/admin`, create a user, then create a Communication for that user. The Communication should first show `pending`, the worker should log that it claimed and sent it, and the refreshed admin page should show `sent`. If MailHog is running, the email should appear at `http://localhost:8025`.
