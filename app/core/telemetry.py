# app/core/telemetry.py
"""
Telemetry & Cost Management: Tracks execution duration, token counts,
and calculates real-time API costs in USD.
"""

import logging
from typing import Dict, Any

# Configure a dedicated telemetry logger
logger = logging.getLogger("app.telemetry")


class TelemetryManager:
    """
    Tracks and logs operational metrics (execution time, tokens, estimated cost).
    Provides observability into platform usage and cost efficiency.
    """
    
    @staticmethod
    def log_operation(
        operation: str,
        duration_sec: float,
        tokens: Dict[str, int],
        model: str = "llama-3.3-70b-versatile",
        success: bool = True
    ) -> Dict[str, Any]:
        """
        Logs a structured telemetry entry and returns the calculated metrics.
        """
        input_tokens = tokens.get("input_tokens", 0)
        output_tokens = tokens.get("output_tokens", 0)
        total_tokens = input_tokens + output_tokens

        # Cost calculation based on standard Llama 3 70B rates on Groq:
        # $0.59 per million input tokens, $0.79 per million output tokens
        input_cost = (input_tokens / 1_000_000) * 0.59
        output_cost = (output_tokens / 1_000_000) * 0.79
        estimated_cost_usd = round(input_cost + output_cost, 6)

        telemetry_data = {
            "operation": operation,
            "duration_seconds": round(duration_sec, 3),
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "success": success
        }

        # Log as structured JSON-like extra context
        logger.info(
            f"📊 Telemetry | {operation} | {duration_sec:.2f}s | {total_tokens} tokens | ${estimated_cost_usd:.6f}",
            extra={"telemetry": telemetry_data}
        )

        return telemetry_data
