# Scrapling

Self-hosted web data platform. Extract structured data from any website, find local business leads, and monitor pages on a schedule. AI-powered extraction means you describe the fields you want in plain language instead of writing selectors.

## Features

- **AI extraction** — describe the fields you want; the model reads the page and returns clean JSON
- **Universal scraper** — CSS selectors or auto content detection, across text, links, images and tables
- **Local lead finder** — pull business leads from map search results
- **Scheduled monitoring** — run any scrape on an interval and detect what changed
- **Bulk operations** — thousands of URLs with parallel workers
- **Instant export** — CSV, Excel and JSON
- **Telegram alerts** — notified when a batch finishes or a page changes

## Quick start

```bash
cp .env.example .env
# edit .env with your AI provider + token
docker compose up -d
```

Then open `http://localhost:8777`.

## Configuration

Set these environment variables (or put them in `.env`):

| Variable | Description |
|----------|-------------|
| `AI_BASE_URL` | OpenAI-compatible API base URL |
| `AI_API_KEY` | API key for the provider |
| `AI_MODELS` | Comma-separated model list (used round-robin) |
| `SCRAP_AUTH_TOKEN` | Dashboard auth token |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | Telegram notifications (optional) |

## API

- `POST /api/scrape` — scrape one URL
- `POST /api/bulk` — queue many URLs
- `POST /api/ai-extract` — fetch a URL and let the model return structured JSON
- `POST /api/analyze/{id}` — ask questions about a finished scrape
- `GET /api/history` — recent scrapes
- `GET /api/stats` — dashboard stats

All endpoints require the `x-token` header (or `?token=`).

## Run without Docker

```bash
python -m venv venv
. venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8777
```

## License

MIT