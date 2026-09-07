# Jarrah Learning — complete no-website-payment build

This is the complete deployable website folder.

Payments are NOT processed by the website. There is no Stripe checkout, card payment, or tutor payout connection. Paid tutoring, where applicable, is arranged separately by e-transfer.

Required Render commands when this folder is uploaded to the repository as `jarrah-learning-complete-no-payments`:

Build:
`cd "jarrah-learning-complete-no-payments" && pip install -r requirements.txt`

Start:
`cd "jarrah-learning-complete-no-payments" && gunicorn app:app`
