# APEX CRUSHER

Automated options trading bot — FastAPI backend connected to the Alpaca paper trading API.

---

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Broker | Alpaca paper trading (alpaca-py) |
| Database | PostgreSQL (via asyncpg / SQLAlchemy) |
| Cache | Redis |
| Hosting | Railway |
| Notifications | Discord webhooks (optional) |

---

## Deploying to Railway

### 1. Fork the repository

Click **Fork** on GitHub. Railway deploys from your own fork so you can push changes without touching the upstream.

### 2. Create a new Railway project

1. Go to [railway.app](https://railway.app) and click **New Project**.
2. Choose **Deploy from GitHub repo** and select your fork.
3. Railway detects `railway.toml` automatically — no extra configuration needed for the build or start command.

### 3. Set environment variables

In the Railway dashboard, open your service → **Variables** tab, then add each variable below.  
Copy `.env.example` as a reference for the expected format.

| Variable | Required | Description |
|---|---|---|
| `ALPACA_API_KEY` | Yes | Alpaca key ID (paper account) |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret key (paper account) |
| `ALPACA_BASE_URL` | Yes | `https://paper-api.alpaca.markets` |
| `DATABASE_URL` | Yes | PostgreSQL connection string (see step 4) |
| `REDIS_URL` | Yes | Redis connection string (see step 5) |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook URL for trade alerts |

> **Note:** Railway automatically injects `PORT` — do not add it manually.

To get your Alpaca credentials:  
[app.alpaca.markets](https://app.alpaca.markets) → switch to **Paper Trading** → **API Keys** → **Generate New Key**.

### 4. Add a PostgreSQL service

1. In your Railway project, click **+ New** → **Database** → **PostgreSQL**.
2. Once it provisions, open the PostgreSQL service → **Connect** tab.
3. Copy the **DATABASE_URL** value and paste it into your app service's Variables.

Railway links services in the same project via private networking — the URL works out of the box.

### 5. Add a Redis service

1. Click **+ New** → **Database** → **Redis**.
2. Open the Redis service → **Connect** tab.
3. Copy the **REDIS_URL** value and paste it into your app service's Variables.

### 6. Deploy

Railway triggers a deployment automatically when variables change or when you push a new commit.  
Watch the build logs in the **Deployments** tab. A successful build ends with:

```
INFO | APEX CRUSHER starting… version=0.1.0
INFO | Alpaca connection verified.
```

### 7. Verify the deployment

Once the deployment is live, Railway displays a public URL (e.g. `https://apex-crusher-production.up.railway.app`).

**Health check:**

```bash
curl https://<your-railway-url>/health
```

Expected response:

```json
{
  "status": "ok",
  "timestamp": "2026-04-23T12:00:00Z",
  "version": "0.1.0",
  "alpaca_connected": true
}
```

`alpaca_connected: true` confirms the API keys are valid and the paper trading endpoint is reachable.

**Bot status:**

```bash
curl https://<your-railway-url>/api/status
```

```json
{
  "bot_status": "stopped",
  "timestamp": "2026-04-23T12:00:00Z"
}
```

**Interactive API docs:**

```
https://<your-railway-url>/docs
```

---

## Running locally

```bash
# 1. Clone your fork
git clone https://github.com/<your-username>/brometheus.git
cd brometheus

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env and fill in your real Alpaca keys, DATABASE_URL, and REDIS_URL

# 5. Start the server
uvicorn backend.api.main:app --reload --host 0.0.0.0 --port 8000
```

Then visit `http://localhost:8000/health` to confirm it is running.

---

## Project structure

```
brometheus/
├── railway.toml              # Railway build + deploy config
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template (safe to commit)
└── backend/
    ├── api/
    │   └── main.py           # FastAPI app, routes, lifecycle
    ├── core/
    │   └── greeks_engine.py  # Black-Scholes pricing + Greeks + IV solver
    ├── data/
    │   └── alpaca_client.py  # Alpaca API wrapper (quotes, options, orders)
    └── utils/
        ├── config.py         # Environment variable loading + validation
        └── logger.py         # Structured logging setup
```

---

## Troubleshooting

**`alpaca_connected: false` in /health**  
Double-check `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `ALPACA_BASE_URL` in Railway Variables. Ensure you are using paper trading credentials from the Paper Trading section of the Alpaca dashboard, not the live trading section.

**App crashes on startup with a `ValueError`**  
A required environment variable is missing or empty. The error message lists every missing variable by name.

**Build fails with a pip error**  
Railway's Nixpacks builder uses Python 3.11 by default. If you need a specific version, add a `runtime.txt` file containing the version string (e.g. `python-3.12`).

**DATABASE_URL / REDIS_URL connection refused**  
Make sure the PostgreSQL and Redis services are in the **same Railway project** as the app service — Railway's private networking only works within a project.
