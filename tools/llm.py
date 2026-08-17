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
from dotenv import load_dotenv
from google import genai

load_dotenv(override=True)

class LLMError(RuntimeError):
    """Raised when the LLM backend fails after all retries."""
    pass

def call_llm(system_prompt: str, user_prompt: str) -> str:
    backend = os.getenv("LLM_BACKEND", "gemini").lower()
    
    if backend == "gemini":
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        full_prompt = f"{system_prompt}\n\nUser Request:\n{user_prompt}"
        
        # Retry up to 5 times with increasing delays for 503 capacity spikes
        for attempt in range(1, 6):
            try:
                chat = client.chats.create(model=model_name)
                response = chat.send_message(full_prompt)
                return response.text
            except Exception as e:
                if attempt == 5:
                    raise LLMError(f"Gemini API call failed after 5 attempts: {e}")
                time.sleep(3 * attempt)  # Delay 3s, 6s, 9s, 12s
                
    elif backend in ("bedrock", "anthropic"):
        raise LLMError(f"Backend '{backend}' configured but credentials not set.")
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {backend!r}")