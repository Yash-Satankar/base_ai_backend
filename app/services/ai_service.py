# app/services/ai_service.py

from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


# ─── Groq client (active) ──────────────────────────────────────
from groq import Groq

_groq_client = None

def get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=settings.GROQ_API_KEY)
        logger.info("✅ Groq client initialised")
    return _groq_client
# ───────────────────────────────────────────────────────────────


# ─── Anthropic client (commented — switch when you have credits) ─
# import anthropic
# _anthropic_client = None
#
# def get_anthropic_client():
#     global _anthropic_client
#     if _anthropic_client is None:
#         _anthropic_client = anthropic.Anthropic(
#             api_key=settings.ANTHROPIC_API_KEY
#         )
#         logger.info("✅ Anthropic client initialised")
#     return _anthropic_client
# ───────────────────────────────────────────────────────────────


def generate_schema(
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """
    Central function to generate schema using the configured AI provider.
    Switch provider by changing AI_PROVIDER in .env
    """

    provider = settings.AI_PROVIDER.lower()
    logger.info(f"🤖 Generating schema using provider: {provider}")

    if provider == "groq":
        return _generate_with_groq(system_prompt, user_prompt)

    # elif provider == "anthropic":
    #     return _generate_with_anthropic(system_prompt, user_prompt)

    else:
        raise ValueError(f"Unknown AI_PROVIDER: {provider}. Use 'groq' or 'anthropic'.")


def _generate_with_groq(system_prompt: str, user_prompt: str) -> dict:
    """Generate using Groq (Llama 3.3 70B) — free tier."""
    client = get_groq_client()

    try:
        response = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        max_tokens=settings.MAX_TOKENS,
        temperature=0.2,   # low temperature = more deterministic SQL
        timeout=60,
        )
    except Exception as e:
        if "timeout" in str(e).lower():
            raise HTTPException(
                status_code=504,
                detail="AI generation timed out. Please try again."
            )
        raise

    content = response.choices[0].message.content
    input_tokens = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens

    logger.info(f"✅ Groq response: {input_tokens} in / {output_tokens} out tokens")

    return {
        "content": content,
        "provider": "groq",
        "model": settings.GROQ_MODEL,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ─── Anthropic implementation — uncomment when you have credits ──
#
# def _generate_with_anthropic(system_prompt: str, user_prompt: str) -> dict:
#     """Generate using Claude Sonnet — best quality, paid."""
#     client = get_anthropic_client()
#
#     response = client.messages.create(
#         model=settings.ANTHROPIC_MODEL,
#         max_tokens=settings.MAX_TOKENS,
#         system=system_prompt,
#         messages=[
#             {
#                 "role": "user",
#                 "content": user_prompt,
#             }
#         ],
#         temperature=0.2,
#     )
#
#     content = response.content[0].text
#     input_tokens = response.usage.input_tokens
#     output_tokens = response.usage.output_tokens
#
#     logger.info(f"✅ Anthropic response: {input_tokens} in / {output_tokens} out tokens")
#
#     return {
#         "content": content,
#         "provider": "anthropic",
#         "model": settings.ANTHROPIC_MODEL,
#         "usage": {
#             "input_tokens": input_tokens,
#             "output_tokens": output_tokens,
#         },
#     }
# ───────────────────────────────────────────────────────────────