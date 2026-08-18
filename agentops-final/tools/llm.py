"""Single entry point for calling the reasoning LLM. Backed by one of:
  - Amazon Bedrock (LLM_BACKEND=bedrock)   -- extra AWS surface area for judging
  - Anthropic API directly (LLM_BACKEND=anthropic) -- simplest to set up
  - Gemini (LLM_BACKEND=gemini)            -- useful for local dev without AWS/Anthropic access

Each backend's SDK is imported lazily, inside its branch, not at module load
time. That matters: previously this file did `from google import genai` at
the top unconditionally, so *any* backend -- even bedrock or anthropic --
would crash on import if the google-genai package wasn't installed. Lazy
imports mean you only need the SDK for the backend you've actually chosen.

The LLM sits on the incident critical path, so calls are wrapped with a
bounded retry/backoff. On exhaustion we raise ``LLMError`` -- the diagnostic
agent catches it and falls back to vector memory rather than crashing.
"""
import os
import time

from dotenv import load_dotenv

load_dotenv(override=True)

LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "30"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))


class LLMError(RuntimeError):
    """Raised when the LLM backend fails after all retries."""
    pass


def call_llm(system_prompt: str, user_prompt: str) -> str:
    backend = os.getenv("LLM_BACKEND", "gemini").lower()

    if backend == "gemini":
        return _call_gemini(system_prompt, user_prompt)
    elif backend == "bedrock":
        return _call_bedrock(system_prompt, user_prompt)
    elif backend == "anthropic":
        return _call_anthropic(system_prompt, user_prompt)
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {backend!r}")


def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    try:
        from google import genai
    except ImportError as e:
        raise LLMError(
            "LLM_BACKEND=gemini but the google-genai package isn't installed "
            "(pip install google-genai)."
        ) from e

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    full_prompt = f"{system_prompt}\n\nUser Request:\n{user_prompt}"

    last_err = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            chat = client.chats.create(model=model_name)
            response = chat.send_message(full_prompt)
            return response.text
        except Exception as e:
            last_err = e
            if attempt == LLM_MAX_RETRIES:
                raise LLMError(f"Gemini API call failed after {LLM_MAX_RETRIES} attempts: {e}") from e
            time.sleep(3 * attempt)
    raise LLMError(f"Gemini API call failed: {last_err}")


def _call_bedrock(system_prompt: str, user_prompt: str) -> str:
    import json

    import boto3

    model_id = os.getenv("BEDROCK_MODEL_ID")
    if not model_id:
        raise LLMError(
            "LLM_BACKEND=bedrock but BEDROCK_MODEL_ID is not set -- check the "
            "exact current model ID string in your Bedrock console -> Model access."
        )

    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    )

    last_err = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            resp = client.invoke_model(modelId=model_id, body=body)
            payload = json.loads(resp["body"].read())
            return payload["content"][0]["text"]
        except Exception as e:
            last_err = e
            if attempt == LLM_MAX_RETRIES:
                raise LLMError(f"Bedrock call failed after {LLM_MAX_RETRIES} attempts: {e}") from e
            time.sleep(2 * attempt)
    raise LLMError(f"Bedrock call failed: {last_err}")


def _call_anthropic(system_prompt: str, user_prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise LLMError(
            "LLM_BACKEND=anthropic but the anthropic package isn't installed "
            "(pip install anthropic)."
        ) from e

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("LLM_BACKEND=anthropic but ANTHROPIC_API_KEY is not set.")

    client = Anthropic(api_key=api_key, timeout=LLM_TIMEOUT_S)
    # Check your Anthropic console for which model IDs your account can access
    # before relying on this default.
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    last_err = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            last_err = e
            if attempt == LLM_MAX_RETRIES:
                raise LLMError(f"Anthropic API call failed after {LLM_MAX_RETRIES} attempts: {e}") from e
            time.sleep(2 * attempt)
    raise LLMError(f"Anthropic API call failed: {last_err}")
