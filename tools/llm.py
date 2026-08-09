"""Single entry point for calling the reasoning LLM. Backed by either:
  - Amazon Bedrock (LLM_BACKEND=bedrock)  -- extra AWS surface area for judging
  - Anthropic API directly (LLM_BACKEND=anthropic) -- simplest to set up

Both paths use a Claude model so the diagnostic agent's prompt doesn't need
to change based on backend.

The LLM sits on the incident critical path, so calls are wrapped with a
bounded retry/backoff. On exhaustion we raise ``LLMError`` -- the diagnostic
agent catches it and falls back to vector memory rather than crashing.
"""
import os
import time


class LLMError(RuntimeError):
    """Raised when the LLM backend fails after all retries."""


LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "30"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))


def call_llm(system_prompt: str, user_prompt: str) -> str:
    backend = os.getenv("LLM_BACKEND", "bedrock").lower()

    if backend == "bedrock":
        fn = _call_bedrock
    elif backend == "anthropic":
        fn = _call_anthropic
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {backend!r} (use 'bedrock' or 'anthropic')")

    last_err = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            return fn(system_prompt, user_prompt)
        except Exception as e:  # noqa: BLE001 -- backend SDKs raise varied types
            last_err = e
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s ...
    raise LLMError(f"LLM call failed after {LLM_MAX_RETRIES} attempts: {last_err}")


def _call_bedrock(system_prompt: str, user_prompt: str) -> str:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(read_timeout=LLM_TIMEOUT_S, connect_timeout=LLM_TIMEOUT_S, retries={"max_attempts": 0}),
    )
    model_id = os.getenv("BEDROCK_MODEL_ID")
    if not model_id:
        raise RuntimeError("BEDROCK_MODEL_ID is not set -- check the model catalog in your Bedrock console")

    resp = client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
    )
    return resp["output"]["message"]["content"][0]["text"]


def _call_anthropic(system_prompt: str, user_prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic(timeout=LLM_TIMEOUT_S)  # reads ANTHROPIC_API_KEY from env
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text
