# Deployment Guide — Posit+EV

## 1. Deploy to Railway

### 1a. Create the service

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select this repository
3. Railway auto-detects `railway.toml` and runs:
   ```
   uvicorn web.main:app --host 0.0.0.0 --port $PORT
   ```

### 1b. Set environment variables in Railway

In **Settings → Variables**, add every key from your `.env`:

| Variable | Value |
|---|---|
| `ODDS_API_KEY` | your key |
| `TELEGRAM_BOT_TOKEN` | your token |
| `TELEGRAM_CHAT_ID` | your chat id |
| `STRIPE_SECRET_KEY` | `sk_live_…` |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` |
| `STRIPE_PRICE_ID` | `price_1TFjJhKD40rJh2vKtd7KyMAs` |
| `RESEND_API_KEY` | your key |
| `JWT_SECRET` | a long random string (32+ chars) |
| `BASE_URL` | `https://www.posit-ev.com` |
| `DATABASE_URL` | Postgres URL (see §3 below) |
| `LOG_LEVEL` | `INFO` |

> **Generate a strong JWT_SECRET:**
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

### 1c. Add a PostgreSQL database (recommended for production)

1. In your Railway project → **+ New** → **Database** → **PostgreSQL**
2. Railway injects `DATABASE_URL` automatically — or copy it from the Postgres service's **Variables** tab and add it manually

---

## 2. Custom domain on Railway

1. In Railway → your service → **Settings** → **Domains** → **+ Custom Domain**
2. Enter `www.posit-ev.com` and click **Add**
3. Railway will display a **CNAME target** — copy it. It looks like:
   ```
   your-service-production-xxxx.up.railway.app
   ```
   Keep this tab open; you'll paste it into Cloudflare next.

---

## 3. Cloudflare DNS records

> **Required:** Your domain `posit-ev.com` must be using **Cloudflare nameservers**.
>
> ⚠️ Set the proxy toggle to **DNS only (gray cloud)** for both records below.
> Railway manages its own SSL via Let's Encrypt. Cloudflare's orange-cloud proxy
> intercepts TLS and can break Railway's certificate provisioning.

### Records to add

| Type | Name | Value | TTL | Proxy |
|------|------|-------|-----|-------|
| `CNAME` | `www` | `your-service-production-xxxx.up.railway.app` | Auto | **DNS only** ☁️ |
| `CNAME` | `posit-ev.com` (apex) | `your-service-production-xxxx.up.railway.app` | Auto | **DNS only** ☁️ |

Cloudflare supports a CNAME at the apex domain via **CNAME flattening** — this is
automatic and requires no special configuration on your end.

### Redirect apex → www (recommended)

Add a Cloudflare **Redirect Rule** so bare `posit-ev.com` always goes to `www`:

1. **Cloudflare Dashboard** → your domain → **Rules** → **Redirect Rules** → **Create rule**
2. Configure:
   - **When:** Hostname equals `posit-ev.com`
   - **Then:** Static redirect → `https://www.posit-ev.com` → **301 Permanent**
3. Save and deploy

---

## 4. SSL / TLS settings

Because both records are **DNS only (gray cloud)**:

- Railway provisions a Let's Encrypt certificate for `www.posit-ev.com` automatically
- No action needed in Cloudflare SSL/TLS settings for these hostnames
- DNS propagation typically takes **2–10 minutes** with Cloudflare

If you later switch to **Cloudflare proxy (orange cloud)** for DDoS protection:
1. Go to **SSL/TLS** → **Overview** → set mode to **Full** (not "Flexible")
2. Keep it off **Full (strict)** unless Railway's cert is already provisioned

---

## 5. Stripe webhook endpoint

After deploying, register the webhook in your Stripe Dashboard:

1. **Stripe Dashboard** → **Developers** → **Webhooks** → **Add endpoint**
2. URL: `https://www.posit-ev.com/stripe/webhook`
3. Events to listen for:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Copy the **Signing Secret** → paste into Railway env var `STRIPE_WEBHOOK_SECRET`

---

## 6. Verify the deployment

```bash
# Health check
curl -I https://www.posit-ev.com

# Confirm SSL cert
curl -v https://www.posit-ev.com 2>&1 | grep "subject:"

# Check Railway logs
railway logs --tail
```

---

## 7. Local development reference

```bash
# Activate venv
source venv/bin/activate

# Run web server
uvicorn web.main:app --reload

# Run Telegram bot
nohup python telegram_bot.py >> logs/bot.log 2>&1 &

# Run pipeline manually
python main.py --league all --market all --save

# Run scheduler
python scheduler.py
```
