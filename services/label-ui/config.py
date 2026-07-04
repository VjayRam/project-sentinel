from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://sentinel:sentinel@localhost:27017/sentinel"

    airflow_base_url: str = "http://localhost:8090"
    airflow_admin_user: str = "admin"
    airflow_admin_password: str = "sentinel"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
