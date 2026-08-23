"""生成并校验 OpenAPI 路由 operationId 唯一性。"""

from __future__ import annotations

import json

from app.api import create_app
from app.config.settings import Settings


def main() -> None:
    schema = create_app(Settings(environment="test")).openapi()
    operation_ids = [
        operation.get("operationId")
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId")
    ]
    if len(operation_ids) != len(set(operation_ids)):
        raise SystemExit("OpenAPI operationId 重复")
    if "ErrorEnvelope" not in schema.get("components", {}).get("schemas", {}):
        raise SystemExit("OpenAPI 缺少统一错误结构")
    if "ApiKeyAuth" not in schema.get("components", {}).get("securitySchemes", {}):
        raise SystemExit("OpenAPI 缺少 API Key 权限声明")
    if any(
        "429" not in operation.get("responses", {})
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId")
    ):
        raise SystemExit("OpenAPI 缺少限流响应语义")
    print(json.dumps({"paths": len(schema["paths"]), "valid": True}))


if __name__ == "__main__":
    main()
