# Jarrah Learning — Supabase + booking email upgrade

This version keeps the existing Flask site but moves account, profile, availability, booking, assessment and recap data to Supabase Postgres when `DATABASE_URL` is configured.

## Render setup

1. In Supabase, open **Connect** and copy the **Session pooler** connection string. Render is IPv4-only, so do not use Supabase's direct IPv6 database URL.
2. In Render → Jarrah Learning → Environment, create `DATABASE_URL` and paste that pooler string after replacing the password placeholder with your Supabase database password.
3. Set a stable `FLASK_SECRET_KEY`. If this changes, users will be logged out even though their accounts remain safely stored.
4. Keep `APP_URL=https://jarrah-learning.onrender.com`.

## Email notifications

This build supports automatic booking emails through Resend. Set `RESEND_API_KEY` and `EMAIL_FROM` in Render. Without them, bookings still work but emails are skipped.

## Important

Database records are persistent in Supabase. Uploaded files/photos still use Render's local filesystem in this version, so those uploads should be moved to Supabase Storage before relying on them for permanent school records.
