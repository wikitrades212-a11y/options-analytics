# Railway Environment Variables

## REQUIRED

| Variable | Value | Notes |
|---|---|---|
| `DATA_PROVIDER` | `tradier` | Only supported provider |
| `TRADIER_TOKEN` | `<your_token>` | Tradier brokerage API token |
| `TELEGRAM_BOT_TOKEN` | `<your_token>` | From @BotFather |
| `TELEGRAM_CHAT_ID` | `<your_chat_id>` | Your personal or group chat ID |
| `DEDUP_DB_PATH` | `/data/dedup.db` | Requires Railway persistent volume at /data |
| `SPREAD_DB_PATH` | `/data/spread_trades.db` | Same volume as above |

## OPTIONAL (tuning)

| Variable | Default | Notes |
|---|---|---|
| `TRADIER_SANDBOX` | `false` | Set `true` for testing without live data |
| `CACHE_TTL` | `60` | Cache TTL in seconds |
| `SCAN_TICKERS` | `SPY,QQQ,AAPL,TSLA,NVDA,AMZN,MSFT,META,AMD,GOOGL` | Comma-separated base scan list |
| `SCAN_MIN_SCORE` | `60.0` | Minimum unusual_score to consider a contract |
| `SCAN_MIN_PREMIUM` | `100000` | Minimum vol_notional ($) to consider a contract |
| `SCAN_MIN_VOLUME` | `250` | Minimum contract volume |
| `SCAN_TOP_N` | `5` | Top N contracts kept per ticker per scan |
| `LHF_MIN_SCORE` | `80` | Minimum LHF score to classify LOW_HANGING_FRUIT |
| `SPREAD_MIN_SCORE` | `70` | Minimum base spread score to output TAKE |
| `TELEGRAM_ENABLED` | `true` | Master switch for Telegram sending |
| `RATE_LIMIT` | `30` | API requests per minute per IP |
| `CORS_ORIGINS` | `http://localhost:3000` | Not needed if no frontend |

## DEPRECATED — DO NOT SET

These were used by the old Robinhood integration. Remove them if present:
- `RH_USERNAME`
- `RH_PASSWORD`
- `RH_MFA_SECRET`
- `RH_PICKLE_B64`

## Railway Persistent Volume

Create a volume in Railway and mount it at `/data`.
Both `DEDUP_DB_PATH` and `SPREAD_DB_PATH` should point to files inside `/data/`.

Without a volume, cooldown state and trade history reset on every redeploy.

## Deploy Checklist

- [ ] Create GitHub repo, push `~/options-analytics/`
- [ ] New Railway project → "Deploy from GitHub repo"
- [ ] Set root directory: `backend`
- [ ] Set all REQUIRED env vars above
- [ ] Create Railway volume → mount at `/data`
- [ ] Set `DEDUP_DB_PATH=/data/dedup.db`
- [ ] Set `SPREAD_DB_PATH=/data/spread_trades.db`
- [ ] Wait for health check at `/health` → `{"status":"ok","readiness":"ready"}`
- [ ] Send `/help` in Telegram to verify bot is responding
- [ ] Send `/scan` to trigger first manual scan
