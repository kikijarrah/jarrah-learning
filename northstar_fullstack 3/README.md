# Northstar Tutoring — runnable full-stack MVP

This version is not a static mockup. It uses Flask + SQLite and implements real local accounts, password hashing, role-based dashboards, tutor approval, tutor matching, availability, bookings, uploads, assessment dates, cancellation logic, tutor recaps, admin visibility, Stripe Connect hooks and Zoom meeting creation hooks.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open http://127.0.0.1:5000

Default admin if you do not change `.env`:
- email: admin@example.com
- password: ChangeThisPassword123!

Change these before any real deployment.

## Real payments

Add a Stripe platform secret key and webhook secret to `.env`. Tutors click **Connect payouts**, which creates a Stripe Express connected account and sends them through onboarding. Paid bookings use Stripe Checkout with a $25 CAD charge, a $4 CAD `application_fee_amount`, and the remainder routed to the tutor's connected Stripe account. The `/stripe/webhook` endpoint confirms bookings only after `checkout.session.completed`.

For local Stripe webhook testing, use the Stripe CLI and forward events to `/stripe/webhook`.

## Real Zoom links

Create a Zoom Server-to-Server OAuth app, give it the required meeting-management scope, then add `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, and `ZOOM_HOST_USER_ID` to `.env`. Confirmed bookings then call Zoom's meeting API and save separate student join and tutor start URLs.

## Before public launch

This MVP still needs production infrastructure rather than just code: deploy it on a host, use Postgres instead of SQLite, put uploads in private object storage, enable HTTPS, add email verification/password reset, CSRF protection, rate limiting, backups, audit logs, error monitoring and transactional email. If minors will use the service, have privacy/consent, safeguarding, tutor screening, record-retention and terms reviewed for your jurisdiction. Define the exact late-cancellation fee; the current code records the full $25 amount when a paid student cancels inside 24 hours, but it does not automatically create a separate Stripe charge.
