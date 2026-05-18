# payment-demo Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock` — controls whether payments hit the real gateway. Must be `mock` in non-production environments. Changes require a PR.

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — operator-toggleable new checkout flow. Safe to flip without a redeploy.
