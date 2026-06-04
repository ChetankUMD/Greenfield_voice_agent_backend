import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://localhost/greenfield")
VAPI_API_KEY: str = os.getenv("VAPI_API_KEY", "")
VAPI_OUTBOUND_ASSISTANT_ID: str = os.getenv("VAPI_OUTBOUND_ASSISTANT_ID", "")
VAPI_PHONE_NUMBER_ID: str = os.getenv("VAPI_PHONE_NUMBER_ID", "")
PRACTICE_CALLBACK_NUMBER: str = os.getenv("PRACTICE_CALLBACK_NUMBER", "")
VAPI_WEBHOOK_SECRET: str = os.getenv("VAPI_WEBHOOK_SECRET", "")
RETRY_INTERVAL_MINUTES: int = int(os.getenv("RETRY_INTERVAL_MINUTES", "2880"))
PORT: int = int(os.getenv("PORT", "8000"))
