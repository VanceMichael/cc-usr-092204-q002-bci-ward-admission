"""JSON Schema 子集校验器：支撑 contracts/ 中交换契约的本地核对。

仅实现契约用到的子集：type、required、properties、additionalProperties
（布尔或子模式）、items、enum。返回错误列表，空列表表示通过。
"""
from __future__ import annotations


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    schema_type = schema.get("type")

    if schema_type == "object":
        if not isinstance(instance, dict):
            return [f"{path} 应为对象"]
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(f"{path}.{required} 缺失")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key} 未在契约中声明")
            elif isinstance(additional, dict):
                errors.extend(validate(value, additional, f"{path}.{key}"))
    elif schema_type == "array":
        if not isinstance(instance, list):
            return [f"{path} 应为数组"]
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{index}]"))
    elif schema_type == "string":
        if not isinstance(instance, str):
            errors.append(f"{path} 应为字符串")
    elif schema_type == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            errors.append(f"{path} 应为整数")
    elif schema_type == "boolean":
        if not isinstance(instance, bool):
            errors.append(f"{path} 应为布尔值")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path} 取值超出枚举 {schema['enum']}")
    return errors
