"""Tiny JSON-schema subset validator (type, properties, required, enum, items,
additionalProperties) used to reject malformed model output before it can
reach the workflow. Unknown keys are dropped, not trusted."""


class SchemaError(ValueError):
    pass


_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "number": (int, float), "integer": int,
}


def validate(value, schema, path="$"):
    """Returns a cleaned copy of value or raises SchemaError."""
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        ok = False
        for ty in types:
            if ty == "null" and value is None:
                return None
            py = _TYPES.get(ty)
            if py and isinstance(value, py) and not (ty in ("number", "integer") and isinstance(value, bool)):
                ok = True
                break
        if not ok:
            raise SchemaError(f"{path}: expected {t}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        # Structured outputs do not guarantee enum capitalisation: match case-insensitively.
        match = [e for e in schema["enum"] if isinstance(e, str) and isinstance(value, str) and e.lower() == value.lower()]
        if not match:
            raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
        value = match[0]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"{path}: missing required '{req}'")
        out = {}
        for k, v in value.items():
            if k in props:
                out[k] = validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") not in (False, None) and isinstance(schema["additionalProperties"], dict):
                out[k] = validate(v, schema["additionalProperties"], f"{path}.{k}")
        return out
    if isinstance(value, list) and "items" in schema:
        return [validate(v, schema["items"], f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, float) and schema.get("type") == "number":
        return float(value)
    return value
