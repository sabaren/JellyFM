"""Entry point: python run.py"""
import uvicorn
from backend.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
        log_level=settings.log_level,
    )
