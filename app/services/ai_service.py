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
        if not settings.GROQ_API_KEY:
            raise HTTPException(
                status_code=500,
                detail="GROQ_API_KEY must be set in .env to use the Groq provider "
                       "(or set AI_PROVIDER=ollama / anthropic).",
            )
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
    elif provider == "together":
        return _generate_with_together(system_prompt, user_prompt, max_tokens, model, temperature)
    elif provider == "anthropic":
        return _generate_with_anthropic(system_prompt, user_prompt, max_tokens, temperature)
    elif provider == "ollama":
        return _generate_with_ollama(system_prompt, user_prompt, max_tokens, model, temperature)
    else:
        raise ValueError(f"Unknown AI_PROVIDER: {provider}. Use 'groq', 'together', 'anthropic' or 'ollama'.")


def _wants_json(system_prompt: str) -> bool:
    """The L1-L8 compile and clarify prompts all say 'valid JSON'; the SQL
    generator says 'Raw MySQL ... ONLY'. Use that to decide when to ask the
    model for guaranteed-parseable JSON."""
    return "valid json" in (system_prompt or "").lower()


def _generate_with_groq(system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None,
                        model: Optional[str] = None, temperature: Optional[float] = None) -> dict:
    """Generate using Groq — with multi-model fallback and 429/413 retry logic."""
    client = get_groq_client()
    temp = 0.2 if temperature is None else temperature

    # Fallback model chain to ensure extremely high availability on free tier quotas.
    # NOTE: Groq decommissioned the llama-3.x ids in 2025; the current lineup on
    # the free tier is the gpt-oss and qwen3 families. qwen3 emits large
    # <think> blocks that get truncated at low max_tokens (→ empty content), so
    # it sits last.
    models_to_try = [
        model or settings.GROQ_MODEL,   # forced model (cost-degrade) or default
        settings.GROQ_MODEL,
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
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
                # Capping max_tokens for smaller / reasoning models to prevent
                # TPM/window overflows and (for qwen3) think-block truncation.
                if max_tokens is not None:
                    max_tokens_to_use = max_tokens
                elif model_to_use == "openai/gpt-oss-20b":
                    max_tokens_to_use = 6000
                elif model_to_use.startswith("qwen/"):
                    max_tokens_to_use = 8000
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
                is_too_large = "413" in err or "request too large" in err or "reduce your message size" in err
                is_rate_limit = "429" in err or "rate limit" in err or getattr(e, "status_code", None) == 429 or is_too_large

                # A 413 means this single request exceeds the model's per-minute
                # window — retrying the identical request is futile, drop to the
                # next (smaller) model straight away.
                if is_too_large:
                    logger.warning(f"⚠️ Groq request too large for {model_to_use} — trying next model. {e}")
                    break

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

                        # Default keeps API latency bounded; batch/back-office
                        # runs raise GROQ_MAX_RATELIMIT_SLEEP to actually pace to
                        # the free-tier TPM window instead of skipping models.
                        max_sleep = settings.GROQ_MAX_RATELIMIT_SLEEP
                        if sleep_time > max_sleep:
                            logger.warning(f"⚠️ Groq rate limit sleep {sleep_time:.1f}s > cap {max_sleep:.0f}s on {model_to_use}. Skipping model.")
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


# ─── Together AI (OpenAI-compatible REST, via httpx) ───────────

def _generate_with_together(system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None,
                            model: Optional[str] = None, temperature: Optional[float] = None) -> dict:
    """Generate using Together AI's OpenAI-compatible /chat/completions endpoint.

    Same model chain intent as Groq — ``openai/gpt-oss-120b`` primary,
    ``openai/gpt-oss-20b`` fallback — both hosted by Together. No extra SDK:
    Together speaks the OpenAI wire format, so a plain httpx POST is enough.
    """
    import httpx

    if not settings.TOGETHER_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="TOGETHER_API_KEY must be set in .env to use the Together provider.",
        )

    temp = 0.2 if temperature is None else temperature
    base = settings.TOGETHER_BASE_URL.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.TOGETHER_API_KEY}"}

    models_to_try = [
        model or settings.TOGETHER_MODEL,   # forced model (cost-degrade) or default
        settings.TOGETHER_MODEL,
        "openai/gpt-oss-20b",
    ]
    seen, model_chain = set(), []
    for m in models_to_try:
        if m and m not in seen:
            seen.add(m)
            model_chain.append(m)

    # Together's gpt-oss endpoints 5xx fairly often; give the primary model
    # several attempts before dropping to the smaller fallback.
    retries_per_model = 4
    last_exception = None

    for model_to_use in model_chain:
        for attempt in range(retries_per_model + 1):
            try:
                payload = {
                    "model": model_to_use,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "temperature": temp,
                    "max_tokens": max_tokens if max_tokens is not None else settings.MAX_TOKENS,
                }
                # gpt-oss reasoning tokens are billed against max_tokens; at the
                # default effort a dense batch prompt can spend the entire
                # budget "thinking" and return zero SQL. Force it low.
                if "gpt-oss" in model_to_use and settings.TOGETHER_REASONING_EFFORT:
                    payload["reasoning_effort"] = settings.TOGETHER_REASONING_EFFORT
                resp = httpx.post(url, json=payload, headers=headers,
                                  timeout=settings.TOGETHER_TIMEOUT_SECONDS)
                if resp.status_code == 429:
                    raise RuntimeError(f"429 rate limit: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()

                choice = data["choices"][0]
                content = (choice["message"].get("content") or "")
                usage = data.get("usage", {}) or {}
                in_tokens = usage.get("prompt_tokens", 0)
                out_tokens = usage.get("completion_tokens", 0)
                finish = choice.get("finish_reason")

                # Reasoning models emit <think>…</think>; drop it (mirrors Groq path).
                if content:
                    content = re.sub(r"<think>.*?(?:</think>|$)", "", content, flags=re.DOTALL).strip()

                # gpt-oss can burn the whole budget on reasoning and return no
                # visible text (finish_reason='length', content empty). Treat as
                # a retryable failure so the loop retries / falls to the smaller
                # model rather than handing back an empty batch.
                if not content:
                    raise RuntimeError(
                        f"empty content from {model_to_use} (finish={finish}, "
                        f"completion_tokens={out_tokens})"
                    )

                logger.info(f"✅ Together: {in_tokens} in / {out_tokens} out tokens (Model: {model_to_use})")
                return {
                    "content":  content,
                    "provider": "together",
                    "model":    model_to_use,
                    "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
                }

            except Exception as e:
                last_exception = e
                err = str(e).lower()
                is_rate_limit = "429" in err or "rate limit" in err
                # Together's gpt-oss endpoints intermittently 5xx; a same-model
                # retry almost always clears it and keeps us on the larger model
                # instead of silently dropping to the 20b fallback.
                is_transient_5xx = any(c in err for c in
                                       ("500 internal server error", "502 bad gateway",
                                        "503 service unavailable", "504 gateway timeout",
                                        "overloaded", "temporarily unavailable"))
                is_timeout = any(c in err for c in
                                 ("timed out", "timeout", "readtimeout", "connecttimeout",
                                  "read operation"))
                is_empty = "empty content from" in err
                if (is_rate_limit or is_transient_5xx or is_timeout or is_empty) and attempt < retries_per_model:
                    m = re.search(r"try again in (\d+(?:\.\d+)?)s", err)
                    sleep_time = min(float(m.group(1)) + 0.5 if m else 2.0 * (2 ** attempt) + 1.0,
                                     settings.GROQ_MAX_RATELIMIT_SLEEP)
                    kind = ("rate limit" if is_rate_limit else "timeout" if is_timeout
                            else "empty response" if is_empty else "server error")
                    logger.warning(f"⚠️ Together {kind} on {model_to_use}. Retrying in {sleep_time:.1f}s… ({str(e)[:120]})")
                    time.sleep(sleep_time)
                    continue
                logger.error(f"❌ Together error with model {model_to_use}: {str(e)[:200]}. Trying next model…")
                break

    err = str(last_exception).lower()
    if "timeout" in err or "timed out" in err:
        raise HTTPException(status_code=504, detail="AI generation timed out (Together).")
    raise HTTPException(
        status_code=503,
        detail=f"Together AI unavailable across all models: {str(last_exception)[:100]}",
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


# ─── Ollama (local, OpenAI-free) ───────────────────────────────

def _generate_with_ollama(system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None,
                          model: Optional[str] = None, temperature: Optional[float] = None) -> dict:
    """Generate using a locally running Ollama server (no API key).

    Uses the native /api/chat endpoint with stream=false. When the prompt asks
    for JSON we set format="json" so Ollama constrains the output to a valid
    JSON value — this is what stops the L1-L8 compile from crashing on prose.
    """
    import httpx

    base = settings.OLLAMA_BASE_URL.rstrip("/")
    model_to_use = model or settings.OLLAMA_MODEL
    temp = 0.2 if temperature is None else temperature

    options = {
        "temperature": temp,
        "num_ctx": settings.OLLAMA_NUM_CTX,
    }
    if max_tokens is not None:
        options["num_predict"] = max_tokens

    payload = {
        "model": model_to_use,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": options,
    }
    if _wants_json(system_prompt):
        payload["format"] = "json"

    try:
        resp = httpx.post(
            f"{base}/api/chat",
            json=payload,
            timeout=settings.AI_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response is not None else ""
        logger.error(f"❌ Ollama HTTP {e.response.status_code}: {body}")
        raise HTTPException(
            status_code=502,
            detail=f"Ollama returned {e.response.status_code}. Is model '{model_to_use}' pulled? ({body})",
        )
    except Exception as e:
        logger.error(f"❌ Ollama call failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Ollama call failed ({settings.OLLAMA_BASE_URL}): {str(e)[:150]}. Is `ollama serve` running?",
        )

    content = (data.get("message") or {}).get("content", "") or ""
    in_tokens = data.get("prompt_eval_count", 0)
    out_tokens = data.get("eval_count", 0)

    # Reasoning models (deepseek-r1, qwen3, …) emit <think>…</think>; drop it.
    if content:
        content = re.sub(r"<think>.*?(?:</think>|$)", "", content, flags=re.DOTALL).strip()

    logger.info(f"✅ Ollama: {in_tokens} in / {out_tokens} out tokens (Model: {model_to_use})")

    return {
        "content": content,
        "provider": "ollama",
        "model": model_to_use,
        "usage": {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
        },
    }