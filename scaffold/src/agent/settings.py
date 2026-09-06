"""Environment-driven configuration.

The graph definition never reads os.environ directly -- everything flows
through here, so a notebook, a test and the FastAPI container can each build
the same graph against different resources.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "prod"] = "dev"

    # OpenAI-compatible endpoint. Defaults to the Triton-backed inference-api
    # from ../langfuseproject/2.inference, reachable on the llmops-net network.
    inference_base_url: str = "http://inference-api:8080/v1"
    inference_api_key: str = "not-needed"
    model_name: str = "ministral-3b"
    temperature: float = 0.0
    max_tokens: int = 512

    # Empty in dev -> MemorySaver. Set in prod -> AsyncPostgresSaver.
    postgres_url: str = ""

    # Hard ceiling on agent<->tool ping-pong, independent of recursion_limit.
    max_tool_iterations: int = 6

    # Langfuse (same variables as the sibling projects).
    langfuse_host: str = "http://langfuse-web:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    service_name: str = "scaffold-agent"

    @property
    def use_postgres(self) -> bool:
        return bool(self.postgres_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
