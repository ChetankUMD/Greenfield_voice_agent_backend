# Greenfield Cardiology — Voice Agent Backend

Webhook + orchestration backend for a Vapi voice agent serving the fictional **Greenfield Cardiology** practice.

Built with **Python / FastAPI / PostgreSQL**.

## Overview

This service is the tool-layer that Vapi calls during phone sessions:

| Endpoint | Purpose |
|---|---|
| `POST /tools/find_availability` | Check open appointment slots |
| `POST /tools/verify_insurance` | Verify a patient's insurance carrier |
| `POST /tools/book_appointment` | Book and confirm an appointment |
| `POST /referral/start` | Kick off the outbound referral callback sequence |
| `POST /vapi/call-result` | Receive Vapi end-of-call webhooks |
| `GET  /health` | Liveness check |
| `GET  /admin/state` | Dump all DB state (debug / demo) |

---

## Setup

### Prerequisites
- Python 3.11+
- PostgreSQL running locally (or a connection string to a remote instance)

### Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

`.env` keys:

| Key | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (e.g. `postgresql://localhost/greenfield`) |
| `VAPI_API_KEY` | Your Vapi API key |
| `VAPI_PHONE_NUMBER_ID` | Phone number ID for outbound calls |
| `VAPI_OUTBOUND_ASSISTANT_ID` | Assistant ID used for outbound referral calls |
| `VAPI_WEBHOOK_SECRET` | Secret set in the Vapi dashboard to authenticate webhooks |
| `PRACTICE_CALLBACK_NUMBER` | Callback number read in voicemail (`415-555-0120`) |
| `RETRY_INTERVAL_MINUTES` | Referral retry gap (default `2880` = 48 h; set `2` for demo) |
| `PORT` | Server port (default `8000`; Render sets this automatically) |

> **Dev mode:** if `VAPI_API_KEY`, `VAPI_PHONE_NUMBER_ID`, or `VAPI_OUTBOUND_ASSISTANT_ID` are blank, outbound calls are stubbed — the scheduler and endpoints still work without real Vapi credentials.

### Create the database

```bash
createdb greenfield
```

---

## Running

### Development (live reload)

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

The app seeds the database automatically on first start with:
- Open slots for the next 10 business days (all 3 providers)
- James Patterson referral record (ready for outbound callback demo)

### Production

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Deploying to Render

A `render.yaml` is included. Push to GitHub, then:

1. In the Render dashboard click **New → Blueprint** and connect this repo.
2. Render will create a **Web Service** and a **PostgreSQL** database.
3. Set the secret env vars in the Render dashboard (`VAPI_API_KEY`, `VAPI_PHONE_NUMBER_ID`, `VAPI_OUTBOUND_ASSISTANT_ID`, `VAPI_WEBHOOK_SECRET`, `PRACTICE_CALLBACK_NUMBER`).
4. Deploy — the app seeds itself on first boot.

---

## Exposing to Vapi (local dev)

### Using ngrok

```bash
ngrok http 8000
```

Use the ngrok URL as the base URL for all Vapi tool endpoints.

---

## Vapi Tool Configuration

Paste these tool webhook URLs into your Vapi assistant configuration (replace `BASE_URL` with your tunnel or Render URL):

```
POST BASE_URL/tools/find_availability
POST BASE_URL/tools/verify_insurance
POST BASE_URL/tools/book_appointment
```

### Tool parameter schemas

#### `find_availability`
```json
{
  "provider":       "Dr. Sarah Chen | Dr. Marcus Webb | Jennifer Park, NP",
  "location":       "SF | Oakland",
  "appt_type":      "new_patient | follow_up | urgent_follow_up | stress_test | np_intake",
  "preferred_date": "YYYY-MM-DD (optional)"
}
```

#### `verify_insurance`
```json
{
  "carrier": "free text, e.g. Aetna PPO"
}
```

#### `book_appointment`
```json
{
  "patient_name":      "Full name",
  "dob":               "YYYY-MM-DD",
  "phone":             "phone number",
  "provider":          "Dr. Sarah Chen | Dr. Marcus Webb | Jennifer Park, NP",
  "location":          "SF | Oakland",
  "appt_type":         "new_patient | follow_up | ...",
  "slot_start_iso":    "ISO 8601 start time (from find_availability result)",
  "insurance_carrier": "required for new_patient / np_intake",
  "is_new_patient":    true
}
```

---

## Outbound Referral Demo

The seeded James Patterson record is ready for the callback demo.

### Step 1 — Compress the retry interval

In `.env`:
```
RETRY_INTERVAL_MINUTES=2
```

### Step 2 — Start the server

```bash
uvicorn app.main:app --reload
```

### Step 3 — Trigger attempt 1

```bash
curl -s -X POST http://localhost:8000/referral/start | python3 -m json.tool
```

With `RETRY_INTERVAL_MINUTES=2`, the scheduler places attempt 2 after 2 minutes and attempt 3 after 4 minutes.

### Step 4 — Simulate call outcomes

```bash
# Simulate voicemail (paste referral_id from step 3)
curl -s -X POST http://localhost:8000/vapi/call-result \
  -H 'Content-Type: application/json' \
  -d '{"message":{"type":"end-of-call-report","call":{"endedReason":"voicemail","metadata":{"referralId":"PASTE_ID_HERE"}}}}'
```

### Step 5 — Watch state

```bash
curl -s http://localhost:8000/admin/state | python3 -m json.tool
```

---

## PHI Notes

- Voicemail is PHI-free: *"This is a message from Greenfield Cardiology. Please call us back at 415-555-0120. Thank you."*
- The `/vapi/call-result` handler never echoes PHI in logs.
- No patient data is returned from tool endpoints beyond what the voice agent needs to read aloud.

---

## Project Structure

```
app/
  main.py              Entry point — FastAPI app + lifespan (DB init, seed, scheduler)
  config.py            Env var loading
  db.py                SQLAlchemy engine + session
  models.py            Slot, Appointment, Referral ORM models
  seed.py              Slot + referral seed data
  scheduler.py         APScheduler referral retry loop
  vapi.py              Outbound call placement (Vapi API client)
  insurance.py         Insurance carrier verification logic
  scheduling.py        Slot finder, date formatters, confirmation IDs
  tool_response.py     Vapi tool-call request/response helpers
  routes/
    health.py          GET /health
    admin.py           GET /admin/state
    tools.py           POST /tools/*
    referral.py        POST /referral/start, POST /referral/{id}/reset
    vapi.py            POST /vapi/call-result
```
