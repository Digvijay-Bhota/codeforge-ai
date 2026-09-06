
from pydantic import BaseModel


class ModelUsage(BaseModel):
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    request_count: int = 1
    latency_ms: int | None = None
    usage_available: bool = True
