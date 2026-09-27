from pydantic import BaseModel, Field, model_validator


class TenantIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class SimulatedTokens(BaseModel):
    input_tokens: int = Field(ge=0, le=2_000_000)
    cached_input_tokens: int = Field(default=0, ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=500_000)
    reasoning_tokens: int = Field(default=0, ge=0, le=500_000)

    @model_validator(mode="after")
    def cached_within_input(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens is part of input_tokens and cannot exceed it")
        return self


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    tokens: SimulatedTokens | None = Field(default=None, description="Simulated token counts; derived from the prompt if omitted")
