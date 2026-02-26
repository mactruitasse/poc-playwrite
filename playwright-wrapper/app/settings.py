from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # URL de Browserless (le service K8s)
    browserless_url: str = "ws://browserless:3000"
    log_level: str = "INFO"

settings = Settings()
