"""启动 FastAPI 服务。"""

import uvicorn
from app.config.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
