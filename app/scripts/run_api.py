"""启动 FastAPI 服务，兼容模块与脚本两种执行方式。"""

import sys
from pathlib import Path

import uvicorn

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
