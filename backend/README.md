# Africa Safari Guide — Backend

A minimal, honest inquiry-intake service. It receives the "SEND INQUIRY"
submissions from the Android app's `InquiryDialog`, validates and stores
them, and gives you a place your team can actually see and act on them —
instead of inquiries living only inside each user's local on-device Room
database, invisible to you.

## What this is NOT

- Not a payment processor. No card details are collected or charged here.
- Not a live-inventory or availability system. It does not check whether an
  operator actually has space on the requested dates.
- Not a replacement for a human reviewing each inquiry and following up.

This exists to fix one specific problem: previously, "CONFIRM BOOKING" only
wrote to the user's local database, and no one on your team ever saw it
unless the user happened to contact you directly. Now it also reaches a
server.

## Local setup

```bash
cd backend
npm install
cp .env.example .env
# edit .env and set a real ADMIN_API_KEY (see .env.example for how to
# generate one)
npm start
```

The server starts on `http://localhost:3000`. Check it's alive:

```bash
curl http://localhost:3000/health
```

## API

### `POST /api/inquiries` (public — no auth)

Submits a new inquiry. Called by the Android app's `InquiryApiClient`.

```bash
curl -X POST http://localhost:3000/api/inquiries \
  -H "Content-Type: application/json" \
  -d '{
    "localBookingId": 1,
    "operatorId": "id_2",
    "operatorName": "Nomad Kenya",
    "itineraryTitle": "Masai Mara Family",
    "totalPrice": 8200,
    "travelerName": "Jane Doe",
    "travelerEmail": "jane@example.com",
    "travelerPhone": "+1234567890",
    "travelDate": "August 2026",
    "numberOfTravelers": 2,
    "specialRequests": "Vegetarian meals please"
  }'
```

### `GET /api/inquiries` (admin only)

Lists inquiries, optionally filtered by `?status=PENDING` or `?operatorId=id_2`.

```bash
curl http://localhost:3000/api/inquiries \
  -H "Authorization: Bearer YOUR_ADMIN_API_KEY"
```

### `PATCH /api/inquiries/:id/status` (admin only)

Updates status to `PENDING`, `CONFIRMED`, `DECLINED`, or `CANCELLED` — this
is what your team does after manually confirming availability with the
operator.

```bash
curl -X PATCH http://localhost:3000/api/inquiries/1/status \
  -H "Authorization: Bearer YOUR_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "CONFIRMED"}'
```

## Deploying

Any Node host works. Render, Railway, and Fly.io all have free/cheap tiers
and support persistent disks, which you need so `data.sqlite` survives
redeploys.

### Render (example)

1. Push this `backend/` folder to a GitHub repo (or a subfolder of your
   main repo — Render lets you set a root directory).
2. New → Web Service → connect the repo.
3. Build command: `npm install`
4. Start command: `npm start`
5. Add environment variable `ADMIN_API_KEY` with a real generated value.
6. Add a persistent disk mounted at, e.g., `/data`, and set the environment
   variable `DB_PATH=/data/inquiries.sqlite` so your data survives deploys.
7. Once deployed, you'll get a URL like
   `https://africa-safari-guide-backend.onrender.com`.

### Wiring the Android app to this backend

In `InquiryApiClient.kt`, replace:

```kotlin
private const val BACKEND_BASE_URL = "https://REPLACE_ME_WITH_YOUR_BACKEND_URL"
```

with your real deployed URL, e.g.:

```kotlin
private const val BACKEND_BASE_URL = "https://africa-safari-guide-backend.onrender.com"
```

## Adding real payments later

Do **not** wire Stripe directly into `POST /api/inquiries`. That endpoint
runs before any human has confirmed real availability or a final price —
charging a card at that point would recreate the exact "instant booking
with no availability check" problem this whole redesign exists to fix.

The correct point to add payment is after an operator has manually
confirmed an inquiry (`PATCH .../status` → `CONFIRMED`). A natural next
endpoint would be:

```
POST /api/inquiries/:id/payment-link
```

which creates a Stripe Checkout Session for the *confirmed* price and
returns a URL you send to the traveler by email — not something triggered
automatically at inquiry-submission time.

## Adding notifications

Right now, a new inquiry is only visible if someone queries
`GET /api/inquiries`. For a real team workflow, add an outbound
notification inside the `POST /api/inquiries` handler in
`src/routes/inquiries.js` (marked with a `NOTE:` comment at the relevant
spot) — e.g. an email via Resend/SendGrid, or a Slack webhook — so a human
sees each inquiry promptly instead of having to poll.
