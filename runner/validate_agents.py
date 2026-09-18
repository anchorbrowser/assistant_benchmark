#!/usr/bin/env python3
"""python3 runner/validate_agents.py — validate every agents/<slug>.json against
agents/_schema.json. Zero dependencies. Exits non-zero on any error so it can gate CI.

The schema is a small hand-rolled dialect (see agents/_schema.json), not full JSON
Schema. Supported node types: string, bool, array, object, map. String nodes may carry
`enum`, `enum_ref` (into a named vocab) or `pattern`. Nodes may be `required` and/or
`nullable`.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ROOT / "agents"


def load_schema():
    return json.loads((AGENTS / "_schema.json").read_text())


def check_node(value, node, schema, path, errors):
    t = node.get("type")

    if value is None:
        if node.get("nullable"):
            return
        errors.append(f"{path}: null not allowed")
        return

    if t == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: expected string, got {type(value).__name__}")
            return
        if "enum" in node and value not in node["enum"]:
            errors.append(f"{path}: '{value}' not in {node['enum']}")
        if "enum_ref" in node:
            vocab = schema.get(node["enum_ref"], [])
            if value not in vocab:
                errors.append(f"{path}: '{value}' not in {node['enum_ref']} {vocab}")
        if "pattern" in node and not re.match(node["pattern"], value):
            errors.append(f"{path}: '{value}' does not match {node['pattern']}")

    elif t == "bool":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected bool, got {type(value).__name__}")

    elif t == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: expected array, got {type(value).__name__}")
            return
        item = node.get("items", {})
        for i, v in enumerate(value):
            check_node(v, item, schema, f"{path}[{i}]", errors)

    elif t == "object":
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object, got {type(value).__name__}")
            return
        fields = node.get("fields", {})
        for key, sub in fields.items():
            if key in value:
                check_node(value[key], sub, schema, f"{path}.{key}", errors)
            elif sub.get("required"):
                errors.append(f"{path}.{key}: required field missing")
        for key in value:
            if key not in fields:
                errors.append(f"{path}.{key}: unknown field")

    elif t == "map":
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object, got {type(value).__name__}")
            return
        allowed = schema.get(node.get("keys_ref", ""), None)
        val_node = node.get("value", {})
        for key, v in value.items():
            if allowed is not None and key not in allowed:
                errors.append(f"{path}.{key}: key not in {node['keys_ref']}")
            check_node(v, val_node, schema, f"{path}.{key}", errors)

    else:
        errors.append(f"{path}: unknown schema type '{t}'")


def validate_file(path, schema):
    errors = []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [f"{path.name}: invalid JSON — {e}"], None
    spec = schema["spec"]
    for key, node in spec.items():
        if key in data:
            check_node(data[key], node, schema, key, errors)
        elif node.get("required"):
            errors.append(f"{key}: required field missing")
    for key in data:
        if key not in spec and not key.startswith("$"):
            errors.append(f"{key}: unknown top-level field")
    return errors, data


def main():
    schema = load_schema()
    files = sorted(p for p in AGENTS.glob("*.json") if not p.name.startswith("_"))
    if not files:
        print("no agents/*.json found")
        return 1

    all_errors = []
    slugs = {}
    alias_owner = {}

    for path in files:
        errors, data = validate_file(path, schema)
        for e in errors:
            all_errors.append(f"{path.name}: {e}")
        if data is None:
            continue

        slug = data.get("slug")
        if slug:
            if slug != path.stem:
                all_errors.append(f"{path.name}: slug '{slug}' != filename '{path.stem}'")
            if slug in slugs:
                all_errors.append(f"{path.name}: duplicate slug '{slug}'")
            slugs[slug] = data

        # alias uniqueness: every alias, plus the slug itself, must map to exactly one agent
        for a in [slug] + data.get("aliases", []):
            if a is None:
                continue
            if a in alias_owner and alias_owner[a] != slug:
                all_errors.append(
                    f"{path.name}: alias '{a}' also claimed by '{alias_owner[a]}'")
            alias_owner[a] = slug

    if all_errors:
        print(f"FAIL — {len(all_errors)} problem(s) across {len(files)} file(s):")
        for e in all_errors:
            print(f"  - {e}")
        return 1

    print(f"OK — {len(files)} agent(s), {len(alias_owner)} alias(es), no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
