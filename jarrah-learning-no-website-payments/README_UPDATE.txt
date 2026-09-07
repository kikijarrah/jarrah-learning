JARRAH LEARNING — NO WEBSITE PAYMENT PROCESSING UPDATE

Replace these files in the existing jarrah-learning folder:
- app.py
- requirements.txt
- .env.example
- templates/book.html
- templates/signup_tutor.html
- templates/tutor.html
- templates/admin.html
- templates/home.html

Then delete this old file if it still exists:
- templates/payment_setup_needed.html

What changed:
- Stripe removed
- Website checkout removed
- Tutor payout connection removed
- Admin revenue tracking removed
- Payment confirmation/webhook routes removed
- Late-cancellation fee tracking removed
- Bookings confirm immediately
- Paid tutoring can still be selected, but the website only states that payment is handled separately by e-transfer
- Volunteer tutoring still works
- Zoom creation remains available when Zoom credentials are configured

Important:
Jarrah Learning does not collect, store, or process card/bank payment information.
Any paid tutoring fee is handled separately by e-transfer according to the school's approved process.
