import os

# Set required config before any app module is imported, so module-level
# `os.getenv(...)` reads (e.g. DATABASE_URL, AUTO_APPLY_MIN_CONFIDENCE) don't
# blow up in a bare test environment. No real DB/network is used anywhere in
# this suite -- every DB/LLM/AWS call is mocked in the individual tests.
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:26257/agentops?sslmode=disable")
os.environ.setdefault("LLM_BACKEND", "gemini")
os.environ.setdefault("GEMINI_API_KEY", "fake")
