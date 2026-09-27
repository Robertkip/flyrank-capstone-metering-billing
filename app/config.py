"""Settings from environment (.env). Secrets are never logged."""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/billing.db")
    stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "")          # sk_test_... only (test mode)
    stripe_webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")  # whsec_...
    stripe_price_pro = os.getenv("STRIPE_PRICE_PRO", "")            # price_... (recurring, test mode)
    stripe_api_base = os.getenv("STRIPE_API_BASE", "https://api.stripe.com")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    webhook_tolerance_s = int(os.getenv("WEBHOOK_TOLERANCE_SECONDS", "300"))
    alert_log = os.getenv("ALERT_LOG", "./data/alerts.log")


settings = Settings()
if settings.stripe_secret_key.startswith("sk_live"):
    raise RuntimeError("Live Stripe keys are forbidden - this project runs in test mode only")
