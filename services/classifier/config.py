from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str | None = None

    # Model loading
    model_path: str | None = None
    model_cache_dir: str = "/tmp/sentinel-model-cache"

    # Inference
    classify_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    ort_intra_threads: int = Field(default=4, ge=1, le=32)

    # Batcher
    max_batch_size: int = Field(default=64, ge=1, le=512)
    max_wait_ms: float = Field(default=10.0, ge=0.0)
    max_queue_depth: int = Field(default=1000, ge=1)

    # MinIO
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "sentinel"
    minio_secret_key: str = "sentinel-minio"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
