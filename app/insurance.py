"""Insurance carrier acceptance logic for Greenfield Cardiology."""
from typing import TypedDict

NOT_ACCEPTED_MESSAGE = (
    "We'll need to verify your coverage before confirming an appointment. "
    "Our team will follow up within one business day."
)

_ACCEPTED = [
    "aetna",
    "blue cross blue shield",
    "blue cross",
    "blue shield",
    "bcbs",
    "cigna",
    "united healthcare",
    "unitedhealthcare",
    "united health",
    "medicare",
    "medi-cal",
    "medical",
    "medi cal",
    "health net",
    "healthnet",
]

_NOT_ACCEPTED = [
    "kaiser permanente",
    "kaiser",
    "oscar",
    "covered california",
]


class InsuranceResult(TypedDict):
    accepted: bool
    message: str


def verify_insurance(carrier: str) -> InsuranceResult:
    normalized = carrier.lower().strip()

    for kw in _NOT_ACCEPTED:
        if kw in normalized:
            return {"accepted": False, "message": NOT_ACCEPTED_MESSAGE}

    for kw in _ACCEPTED:
        if kw in normalized or normalized in kw:
            return {
                "accepted": True,
                "message": f"{carrier} is accepted at Greenfield Cardiology. Your insurance is verified.",
            }

    return {"accepted": False, "message": NOT_ACCEPTED_MESSAGE}
