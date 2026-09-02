import time
import re
from typing import Optional
from app.core.config import settings
import logging
from fastapi import HTTPException


logger = logging.getLogger(__name__)


# ─── Groq client (active) ──────────────────────────────────────
from groq import Groq

_groq_client = None

def get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(
            api_key=settings.GROQ_API_KEY,
            max_retries=0
        )
        logger.info("✅ Groq client initialised")
    return _groq_client
# ───────────────────────────────────────────────────────────────


from app.core.resilience import retry_on_failure_sync

@retry_on_failure_sync(retries=3, delay=1.0, backoff=2.0)
def generate_schema(
    system_prompt: str,
    user_prompt: str,
    max_tokens: Optional[int] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> dict:
    """
    Central function to generate schema using the configured AI provider.
    Switch provider by changing AI_PROVIDER in .env

    ``model`` optionally forces a specific model to the front of the
    fallback chain (used by the conversational cost-degrade path).
    ``temperature`` overrides the default 0.2 (used by the output-repair pass).
    """

    provider = settings.AI_PROVIDER.lower()
    logger.info(f"🤖 Generating schema using provider: {provider}")

    if provider == "groq":
        return _generate_with_groq(system_prompt, user_prompt, max_tokens, model, temperature)
    elif provider == "anthropic":
        return _generate_with_anthropic(system_prompt, user_prompt, max_tokens, temperature)
    else:
        raise ValueError(f"Unknown AI_PROVIDER: {provider}. Use 'groq' or 'anthropic'.")


def _generate_with_groq(system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None,
                        model: Optional[str] = None, temperature: Optional[float] = None) -> dict:
    """Generate using Groq — with multi-model fallback and 429/413 retry logic."""
    client = get_groq_client()
    temp = 0.2 if temperature is None else temperature

    # Fallback model chain to ensure extremely high availability on free tier quotas
    models_to_try = [
        model or settings.GROQ_MODEL,   # forced model (cost-degrade) or default
        settings.GROQ_MODEL,
        "llama-3.1-8b-instant",
        "qwen/qwen3.6-27b",
        "openai/gpt-oss-120b"
    ]

    # De-duplicate while preserving order
    seen = set()
    model_chain = []
    for m in models_to_try:
        if m and m not in seen:
            seen.add(m)
            model_chain.append(m)

    retries_per_model = 2
    backoff = 2
    last_exception = None

    for model_to_use in model_chain:
        for attempt in range(retries_per_model + 1):
            try:
                # Capping max_tokens for smaller context / lower-tier models to prevent TPM/window overflows
                if max_tokens is not None:
                    max_tokens_to_use = max_tokens
                elif model_to_use == "llama-3.1-8b-instant":
                    max_tokens_to_use = 5000
                elif model_to_use in ["qwen/qwen3.6-27b", "openai/gpt-oss-120b"]:
                    max_tokens_to_use = 4000
                else:
                    max_tokens_to_use = settings.MAX_TOKENS

                # Clean prompt inputs of potential malicious patterns
                response = client.chat.completions.create(
                    model=model_to_use,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    max_tokens=max_tokens_to_use,
                    temperature=temp,
                    timeout=settings.AI_TIMEOUT_SECONDS,
                )
                
                content    = response.choices[0].message.content
                in_tokens  = response.usage.prompt_tokens
                out_tokens = response.usage.completion_tokens

                # Strip reasoning blocks (<think>...</think>) from reasoning models
                if content:
                    content = re.sub(r'<think>.*?(?:</think>|$)', '', content, flags=re.DOTALL).strip()

                logger.info(f"✅ Groq: {in_tokens} in / {out_tokens} out tokens (Model: {model_to_use})")

                return {
                    "content":  content,
                    "provider": "groq",
                    "model":    model_to_use,
                    "usage": {
                        "input_tokens":  in_tokens,
                        "output_tokens": out_tokens,
                    },
                }

            except Exception as e:
                last_exception = e
                err = str(e).lower()
                is_rate_limit = "429" in err or "rate limit" in err or getattr(e, "status_code", None) == 429 or "413" in err or "too large" in err
                
                if is_rate_limit:
                    # Daily limits (TPD) are persistent, whereas minute/second limits (TPM) should be retried.
                    # If "try again in" is present and wait time is in seconds, it is transient.
                    is_long_limit = ("tpd" in err or "tokens per day" in err) or \
                                    ("try again in" in err and not re.search(r'try again in \d+(\.\d+)?s', err))
                    
                    if is_long_limit:
                        logger.warning(f"⚠️ Groq persistent limit hit on {model_to_use}. Skipping model. Error: {e}")
                        break
                    
                    if attempt < retries_per_model:
                        # Parse suggested wait time from error message
                        wait_match = re.search(r'try again in (\d+(?:\.\d+)?)s', err)
                        if wait_match:
                            sleep_time = float(wait_match.group(1)) + 0.5
                        else:
                            sleep_time = backoff * (2 ** attempt)
                        
                        if sleep_time > 3.0:
                            logger.warning(f"⚠️ Groq rate limit sleep is too long ({sleep_time:.2f}s) on {model_to_use}. Skipping model to avoid blocking.")
                            break
                            
                        logger.warning(f"⚠️ Groq rate limit hit (429) on {model_to_use}. Retrying in {sleep_time:.2f}s... Error: {e}")
                        time.sleep(sleep_time)
                        continue
                
                logger.error(f"❌ Error with model {model_to_use}: {e}. Trying next model...")
                break

    err = str(last_exception).lower()
    if "timeout" in err or "timed out" in err:
        raise HTTPException(
            status_code=504,
            detail=(
                "AI generation timed out across all available models. "
                "Your requirement may be too complex. "
                "Try breaking it into smaller modules."
            ),
        )
    raise HTTPException(
        status_code=503,
        detail=f"AI service unavailable across all models: {str(last_exception)[:100]}",
    )


# ─── Anthropic client (lazy load) ──────────────────────────────
_anthropic_client = None

def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise HTTPException(
                status_code=500,
                detail="ANTHROPIC_API_KEY must be set in .env to use the Anthropic provider."
            )
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
        )
        logger.info("✅ Anthropic client initialised")
    return _anthropic_client


def _generate_with_anthropic(system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None,
                             temperature: Optional[float] = None) -> dict:
    """Generate using Anthropic Claude SDK."""
    client = get_anthropic_client()
    model_to_use = settings.ANTHROPIC_MODEL or "claude-3-5-sonnet-20241022"

    # Cap / handle default tokens
    max_tokens_to_use = max_tokens or settings.MAX_TOKENS
    if max_tokens_to_use > 8192:
        max_tokens_to_use = 8192 # Claude 3.5 Sonnet output token limit

    try:
        response = client.messages.create(
            model=model_to_use,
            max_tokens=max_tokens_to_use,
            temperature=0.2 if temperature is None else temperature,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            timeout=settings.AI_TIMEOUT_SECONDS,
        )
        content = response.content[0].text
        in_tokens = response.usage.input_tokens
        out_tokens = response.usage.output_tokens

        logger.info(f"✅ Anthropic: {in_tokens} in / {out_tokens} out tokens (Model: {model_to_use})")

        return {
            "content":  content,
            "provider": "anthropic",
            "model":    model_to_use,
            "usage": {
                "input_tokens":  in_tokens,
                "output_tokens": out_tokens,
            },
        }
    except Exception as e:
        logger.error(f"❌ Anthropic API call failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API call failed: {str(e)[:150]}"
        )