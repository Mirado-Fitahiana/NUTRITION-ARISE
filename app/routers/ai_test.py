import httpx
from fastapi import APIRouter, HTTPException

from app.core.config import settings

router = APIRouter(prefix="/api/v1/test", tags=["test"])

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@router.get("/openrouter")
async def test_openrouter() -> dict:
    headers = {
        "Authorization": f"Bearer {settings.open_router_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "user", "content": "Reponds juste par: pong"}
        ],
    }

    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(OPENROUTER_URL, headers=headers, json=payload)

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    data = response.json()
    return {
        "model": data.get("model"),
        "reply": data["choices"][0]["message"]["content"],
        "usage": data.get("usage"),
    }
