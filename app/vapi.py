"""Vapi API client — places outbound calls."""
import time
import logging
import httpx
from .config import VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, VAPI_OUTBOUND_ASSISTANT_ID

logger = logging.getLogger(__name__)

_VAPI_CALL_URL = "https://api.vapi.ai/call"


def place_outbound_call(phone_number: str, referral_id: str) -> str:
    """
    Place an outbound call via Vapi and return the Vapi call.id.

    Falls back to a stub ID when API credentials are absent (dev mode).
    Raises RuntimeError if the Vapi API returns an error status.
    """
    if not VAPI_API_KEY or not VAPI_PHONE_NUMBER_ID or not VAPI_OUTBOUND_ASSISTANT_ID:
        stub_id = f"stub-{int(time.time() * 1000)}"
        logger.info(
            "[VAPI STUB] Would call %s for referral %s → stubId=%s",
            phone_number, referral_id, stub_id,
        )
        return stub_id

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            _VAPI_CALL_URL,
            headers={
                "Authorization": f"Bearer {VAPI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "phoneNumberId": VAPI_PHONE_NUMBER_ID,
                "assistantId": VAPI_OUTBOUND_ASSISTANT_ID,
                "customer": {"number": phone_number},
                "metadata": {"referralId": referral_id},
            },
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Vapi call placement failed: {response.status_code} — {response.text}"
        )

    data: dict = response.json()
    return data["id"]
