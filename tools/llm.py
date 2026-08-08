"""Single entry point for calling the reasoning LLM. Backed by either:
  - Amazon Bedrock (LLM_BACKEND=bedrock)  -- extra AWS surface area for judging
  - Anthropic API directly (LLM_BACKEND=anthropic) -- simplest to set up

Both paths use a Claude model so the diagnostic agent's prompt doesn't need
to change based on backend.
"""
import os


def call_llm(system_prompt: str, user_prompt: str) -> str:
    backend = os.getenv("LLM_BACKEND", "bedrock").lower()

    if backend == "bedrock":
        return _call_bedrock(system_prompt, user_prompt)
    elif backend == "anthropic":
        return _call_anthropic(system_prompt, user_prompt)
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {backend!r} (use 'bedrock' or 'anthropic')")


def _call_bedrock(system_prompt: str, user_prompt: str) -> str:
    import boto3

    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
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

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text
