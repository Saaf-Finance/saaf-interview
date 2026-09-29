"""Service configuration. URLs and the model name come from the environment."""

import os

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://mock-llm:8100/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "mock-large")
COMMERCE_URL = os.environ.get("COMMERCE_URL", "http://commerce:8200").rstrip("/")

# Return policy
RETURN_WINDOW_DAYS = 30
APPROVAL_THRESHOLD = 500.0  # refunds above this amount need a manager's approval

# LLM client
LLM_TIMEOUT_S = 30.0
LLM_MAX_ATTEMPTS = 5
LLM_RETRY_DELAY_S = 0.2

# Store backend client
TOOL_TIMEOUT_S = 2.0
TOOL_MAX_ATTEMPTS = 3
TOOL_RETRY_DELAY_S = 0.2

# LangGraph
RECURSION_LIMIT = 200
