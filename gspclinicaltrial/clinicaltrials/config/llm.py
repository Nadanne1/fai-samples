"""Shared LLM factory functions for clinical trial agents.

Two factories:
  get_agent_llm()    — Claude 3.5 Sonnet with PHI guardrail (screening + monitoring)
  get_protocol_llm() — Claude 3.5 Sonnet without guardrail (protocol parsing, high token limit)
"""

import os

from langchain_aws import ChatBedrockConverse

from config.settings import AWS_REGION, PHI_PROTECTION_GUARDRAIL_ID

_AGENT_MODEL = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"


def _get_guardrail_config() -> dict | None:
    """Build guardrail config at call time so env vars are resolved after container start."""
    guardrail_id = PHI_PROTECTION_GUARDRAIL_ID or os.environ.get("PHI_PROTECTION_GUARDRAIL_ID", "")
    if not guardrail_id:
        return None
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": os.environ.get("PHI_GUARDRAIL_VERSION", "1"),
    }


def get_agent_llm() -> ChatBedrockConverse:
    """Return a ChatBedrockConverse instance with PHI guardrail for agent nodes."""
    kwargs: dict = dict(model=_AGENT_MODEL, region_name=AWS_REGION, temperature=0.2, max_tokens=1024)
    guardrail = _get_guardrail_config()
    if guardrail:
        kwargs["guardrails"] = guardrail
    return ChatBedrockConverse(**kwargs)


def get_protocol_llm() -> ChatBedrockConverse:
    """Return a ChatBedrockConverse instance for protocol parsing (no guardrail, high tokens)."""
    return ChatBedrockConverse(
        model=_AGENT_MODEL,
        region_name=AWS_REGION,
        temperature=0.0,
        max_tokens=4096,
    )
