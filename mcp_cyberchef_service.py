#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, TypeAlias
from urllib.error import HTTPError

import argparse
import sys

import requests
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError, BaseModel

from rapidfuzz import fuzz, utils, process

from data_models.cyberchef_pydantic_models import load_definitions
from data_models.tools import GetOperationArgsIn, GetOperationArgsOut, ArgItem, SearchOpsOut, OperationItem, \
    BakeRecipeResponse, RecipeOp, BatchBakeRecipeResponse, ProbeIn, ProbeOut, ValidateRecipeOut, ValidateRecipeIn, \
    SuggestionItem

BYTE_CAP = 1_024
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
MAX_DESC_LEN = 240


CyberChefRecipeOperationT: TypeAlias = BaseModel


def _norm(s: str) -> str:
    return utils.default_process(s or "")


def _tokens(s) -> List[str]:
    return [t for t in _norm(s).split() if t]


def _score_op(q: str, name: str, desc: str) -> float:
    normalized_query, normalized_name, normalized_desc = _norm(q), _norm(name), _norm(desc)
    name_score = fuzz.WRatio(normalized_query, normalized_name)
    desc_score = max(fuzz.partial_token_set_ratio(normalized_query, normalized_desc),
                     fuzz.token_set_ratio(normalized_query, normalized_desc))
    score = 0.75 * name_score + 0.25 * desc_score
    if normalized_query and normalized_name.startswith(normalized_query):
        score += 10
    if normalized_query == normalized_name:
        score += 12
    qtok = _tokens(q)
    ntok = set(_tokens(name))
    if any(t in ntok for t in qtok):
        score += 6
    if qtok and all(t in ntok for t in qtok):
        score += 4
    return max(0, min(100, score))


def load_operations() -> Dict[str, Any]:
    """ The JSON was made half manually because it is not very clean on the CyberChef side """
    current_file = Path(__file__).resolve()
    current_dir = current_file.parent
    with open(os.path.join(current_dir, 'utils', 'js', 'operations.json')) as f:
        return json.load(f)


CYBERCHEF_OPERATIONS = load_operations()

# Defaults (can be overridden via CLI args)
DEFAULT_API_URL = "http://localhost:3000/"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3002

# Parse CLI args early so we can construct MCP with the right host/port before decorators run
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--api-url", dest="api_url", default=DEFAULT_API_URL)
_parser.add_argument("--host", dest="host", default=DEFAULT_HOST)
_parser.add_argument("--port", dest="port", type=int, default=DEFAULT_PORT)
# Use parse_known_args to avoid errors if upstream adds extra args
_args, _unknown = _parser.parse_known_args(sys.argv[1:])

CYBERCHEF_API_URL = _args.api_url
CYBERCHEF_ALLOWED_CATEGORIES = {'Encodings', 'Serialise', 'Default', 'PublicKey', 'Hashing', 'PGP', 'Ciphers',
                                'Shellcode', 'Jq', 'Handlebars', 'Yara', 'Regex', 'Crypto',
                                'Compression', 'URL', 'Code', 'UserAgent', 'Diff', 'Protobuf'}

CyberChefRecipeOperation = load_definitions(CYBERCHEF_OPERATIONS)

# Create an MCP server with CLI-provided host/port
mcp = FastMCP("CyberChef MCP Server",
              host=_args.host,  # used for SSE transport
              port=int(_args.port),
              )


def create_api_request(endpoint: str, request_data: dict) -> dict:
    """
    Send a POST request to one of the CyberChef API endpoints to process request data and retrieve the response

    :param endpoint: API endpoint to retrieve data from
    :param request_data: data to send with the POST request
    :return: dict object of response data
    """
    api_url = f"{CYBERCHEF_API_URL}{endpoint}"
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        print(f"Attempting to send POST request to {api_url}")
        response = requests.post(
            url=api_url,
            headers=request_headers,
            json=request_data
        )
        response.raise_for_status()
        return response.json()
    except HTTPError as req_exc:
        print(f"Exception raised during HTTP POST request to {api_url} - {req_exc}")
        return {"error": f"Exception raised during HTTP POST request to {api_url} - {req_exc}"}


def _slug(s: str) -> str:
    s = s.split(":")[0]  # drop alphabet preview
    s = s.split("(")[0]  # drop RFC etc
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-")


def _enum_table(op: str, arg_name: str) -> Dict[str, str]:
    op_def = CYBERCHEF_OPERATIONS.get(op) or {}
    for a in op_def.get("args", []):
        if a.get("name") == arg_name:
            opts = [str(o) for o in (a.get("options", a.get("value", [])) or [])]
            slugs = [_slug(o) for o in opts]
            counts = Counter(slugs)
            buckets = defaultdict(list)
            for slug, label in zip(slugs, opts):
                buckets[slug].append(label)
            out = {}
            for slug, labels in buckets.items():
                if counts[slug] == 1:
                    out[slug] = labels[0]
                else:
                    for idx, label in enumerate(labels, 1):
                        out[f"{slug}-{idx}"] = label
            return out
    return {}


def _normalize_enum(op: str, arg_name: str, val: Any) -> Any:
    if not isinstance(val, str):
        return val
    tab = _enum_table(op, arg_name)
    key = _slug(val)
    if key in tab:
        return tab[key]
    for k, v in tab.items():
        if k.startswith(key):
            return v
    return val


@mcp.tool()
def get_operation_args(req: GetOperationArgsIn) -> GetOperationArgsOut:
    """
    Return the argument schema for a single CyberChef operation.

    Request schema:
      - op: string (required) — exact CyberChef operation name (case-sensitive)
      - compact: boolean (default true) — if true, enum/option values are returned as short slugs; if false, full labels

    Response:
      { ok: bool, op: string, args: [{ name, type, required, options?, options_total? }], error? }
    """
    op_def = CYBERCHEF_OPERATIONS.get(req.op)
    if not op_def:
        return GetOperationArgsOut(ok=False, error=f"Unknown op: {req.op}")
    out: List[ArgItem] = []
    for a in op_def.get("args", []):
        t = str(a.get("type") or "")
        item = ArgItem(name=str(a.get("name", "")), type=t, required=bool(a.get("required", False)))
        if t in ("enum", "option"):
            opts = a.get("options", a.get("value", [])) or []
            item.options = [_slug(str(o)) for o in opts] if req.compact else [str(o) for o in opts]
            item.options_total = len(opts)
        out.append(item)
    return GetOperationArgsOut(ok=True, op=req.op, args=out)


@mcp.tool()
def search_operations(
        query: str,
        limit: int | Dict[str, int] = None,
        include_args: bool = False,
) -> SearchOpsOut:
    """
    Find relevant CyberChef operations for a short free-text query.

    Request schema:
      - query: string (required) — matched against operation name & description
      - limit: integer 1..20 (default 10)
      - include_args: boolean (default false) — if true, include each op's argument list (large)
    """
    if limit is None:
        limit = 10
    if isinstance(limit, dict):
        try:
            limit = int(limit["value"])  # LibreChat quirk
        except KeyError:
            limit = DEFAULT_LIMIT
    q = (query or "").strip()
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    names = list(CYBERCHEF_OPERATIONS.keys())
    descs = [str(CYBERCHEF_OPERATIONS[n].get("description", "")) for n in names]
    name_matches = process.extract(q, names, scorer=fuzz.WRatio, limit=limit * 2) if q else []
    desc_matches = process.extract(q, descs, scorer=fuzz.partial_token_set_ratio, limit=limit * 2) if q else []

    idxs = {i for _, _, i in name_matches} | {i for _, _, i in desc_matches}
    if not q:
        idxs = set(range(min(limit * 2, len(names))))

    items: List[Dict[str, Any]] = []
    for i in idxs:
        name = names[i]
        op = CYBERCHEF_OPERATIONS[name]
        cat = str(op.get("module", "")).strip()
        if CYBERCHEF_ALLOWED_CATEGORIES and cat and cat not in CYBERCHEF_ALLOWED_CATEGORIES:
            continue
        desc = str(op.get("description", "")).strip()
        if len(desc) > MAX_DESC_LEN:
            desc = desc[:MAX_DESC_LEN - 1] + "…"
        score = int(_score_op(q, name, desc))
        item = {
            "name": name,
            "summary": desc,
            "category": cat or None,
            "score": score,
            "inputType": op.get("inputType", "") or None,
            "outputType": op.get("outputType", "") or None,
        }
        if include_args:
            item["args"] = op.get("args", [])
        items.append(item)

    items.sort(key=lambda x: (-x["score"], x["name"]))
    items = items[:limit]

    out_preview = {"total": len(items), "items": items}
    blob = json.dumps(out_preview, ensure_ascii=False).encode("utf-8")
    trunc = False
    if len(blob) > BYTE_CAP:
        while items and len(items) > 1 and len(json.dumps({"total": len(items), "items": items}, ensure_ascii=False).encode("utf-8")) > BYTE_CAP:
            items.pop()
        trunc = True
    print(f"Returned {len(json.dumps({'total': len(items), 'items': items, 'truncated': trunc}, ensure_ascii=False).encode('utf-8'))} Bytes")
    return SearchOpsOut(total=len(items), items=[OperationItem(**x) for x in items], truncated=trunc)


@mcp.tool()
def bake_recipe(input_data: str, recipe: List[Dict[str, Any]]) -> BakeRecipeResponse:
    """
    Execute a CyberChef recipe on the given input_data.

    Request schema:
      - input_data: string (for binary, Base64-encode first)
      - recipe: array of { op: string, args: object }
    """

    if not recipe:
        return BakeRecipeResponse(ok=False, errors=[
            "No operations to execute. Provide at least one {op, args} object in 'recipe'."
        ])
    errors, validated, warnings = _validate_recipe(recipe)

    if errors:
        return BakeRecipeResponse(ok=False, errors=errors, warnings=warnings)

    request_data = {"input": input_data, "recipe": [op.model_dump() for op in validated]}
    response_data = create_api_request(endpoint="bake", request_data=request_data)

    if isinstance(response_data, dict) and "type" in response_data and "value" in response_data:
        t, v = response_data["type"], response_data["value"]
        if t == "byteArray":
            # noinspection PyBroadException
            try:
                out = bytes(v).decode("utf-8", "ignore")
            except Exception:
                out = ""
            return BakeRecipeResponse(ok=True, output=out, type=t, warnings=warnings)
        if t == "string":
            return BakeRecipeResponse(ok=True, output=str(v), type=t, warnings=warnings)
        return BakeRecipeResponse(ok=True, output=str(v), type=t, warnings=warnings)

    if isinstance(response_data, dict) and "value" in response_data:
        return BakeRecipeResponse(ok=True, output=str(response_data["value"]))

    if isinstance(response_data, dict) and "error" in response_data:
        return BakeRecipeResponse(ok=False, errors=[str(response_data["error"])])

    return BakeRecipeResponse(ok=True, output=str(response_data))


def _validate_recipe(recipe: list[dict[str, Any]]) -> tuple[list[str], list[CyberChefRecipeOperationT], list[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    validated: List[CyberChefRecipeOperation] = []
    for i, operation in enumerate(recipe):
        try:
            op_obj = RecipeOp(**operation)
            if isinstance(op_obj.args, dict):
                op_obj.args = {k: _normalize_enum(op_obj.op, k, v) for k, v in op_obj.args.items()}
            validated.append(CyberChefRecipeOperation(**op_obj.model_dump()))
        except ValidationError as e:
            op_name = operation.get("op") if isinstance(operation, dict) else None
            expected_args = [a.get("name") for a in CYBERCHEF_OPERATIONS.get(op_name, {}).get("args", [])]
            similar = [c[0] for c in
                       process.extract(op_name, list(CYBERCHEF_OPERATIONS.keys()), limit=3)] if op_name else []
            errors.append(
                f"Step {i} invalid for op='{op_name}'. Expected arg keys: {expected_args}. Similar op names: {similar}. Details: {str(e).strip()}"
            )
    return errors, validated, warnings


@mcp.tool()
def batch_bake_recipe(batch_input_data: List[str], recipe: List[Dict[str, Any]]) -> BatchBakeRecipeResponse:
    """Execute one recipe on multiple inputs. Returns {results: List[BakeRecipeResponse]}"""
    errors, validated, warnings = _validate_recipe(recipe)

    if errors:
        return BatchBakeRecipeResponse(results=[BakeRecipeResponse(ok=False, errors=errors)])

    request_data = {"input": batch_input_data, "recipe": [op.model_dump() for op in validated]}
    response_data = create_api_request(endpoint="batch/bake", request_data=request_data)

    results: List[BakeRecipeResponse] = []
    for response in (response_data or []):
        if isinstance(response, dict) and "type" in response and "value" in response:
            t, v = response["type"], response["value"]
            if t == "byteArray":
                # noinspection PyBroadException
                try:
                    out = bytes(v).decode("utf-8", "ignore")
                except Exception:
                    out = ""
                results.append(BakeRecipeResponse(ok=True, output=out, type=t))
            elif t == "string":
                results.append(BakeRecipeResponse(ok=True, output=str(v), type=t))
            else:
                results.append(BakeRecipeResponse(ok=True, output=str(v), type=t))
        elif isinstance(response, dict) and "value" in response:
            results.append(BakeRecipeResponse(ok=True, output=str(response["value"])))
        elif isinstance(response, dict) and "error" in response:
            results.append(BakeRecipeResponse(ok=False, errors=[str(response["error"])]))
        else:
            results.append(BakeRecipeResponse(ok=True, output=str(response)))

    return BatchBakeRecipeResponse(results=results)


@mcp.tool()
def cyberchef_probe(req: ProbeIn) -> ProbeOut:
    """
    Try simple decodes with CyberChef and return the first textlike success.

    Request: { raw_input: string }
    Response: { ok: bool, output?: string, recipe?: [{op,args}], error?: string }
    """
    probes: List[List[Dict[str, Any]]] = [
        [{"op": "From Base64", "args": {}}, {"op": "Gunzip", "args": {}}],
        [{"op": "From Base64", "args": {}}],
        [{"op": "From Hex", "args": {}}, {"op": "Gunzip", "args": {}}],
        [{"op": "From Hex", "args": {}}],
        [{"op": "From Binary", "args": {"Delimiter": "Space", "Byte Length": 8}}],
    ]

    def _looks_textlike(b: bytes) -> bool:
        if not b:
            return False
        bad = sum(c < 9 or (13 < c < 32) or c == 127 for c in b)
        return bad / len(b) < 0.05

    norm_input = req.raw_input

    for r in probes:
        res = bake_recipe(input_data=norm_input, recipe=r)
        if res.ok and res.output and _looks_textlike(res.output.encode("utf-8", "ignore")):
            return ProbeOut(ok=True, recipe=[RecipeOp(**x) for x in r], output=res.output)
    return ProbeOut(ok=False, error="no simple probe succeeded")


@mcp.tool()
def help_bake_recipe() -> Dict[str, Any]:
    """Returns a cheat-sheet for using bake_recipe with schema, tips, and examples."""
    examples = [
        {
            "title": "Decode Base64",
            "request": {"input_data": "SGVsbG8gV29ybGQh", "recipe": [{"op": "From Base64", "args": {}}]},
            "expected": {"ok": True, "output": "Hello World!"},
        },
        {
            "title": "Hex to Base64",
            "request": {
                "input_data": "48656c6c6f",
                "recipe": [
                    {"op": "From Hex", "args": {"Delimiter": "Auto"}},
                    {"op": "To Base64", "args": {}},
                ],
            },
        },
        {
            "title": "Base64 then Gunzip",
            "request": {"input_data": "H4sIAAAAA...",
                        "recipe": [{"op": "From Base64", "args": {}}, {"op": "Gunzip", "args": {}}]},
        },
        {
            "title": "Detect File Type",
            "request": {"input_data": "FF D8 FF E0 ...",
                        "recipe": [{"op": "From Hex", "args": {}}, {"op": "Detect File Type", "args": {}}]},
        },
    ]
    return {
        "overview": "Use bake_recipe with {input_data: str, recipe: [{op: str, args: object}...]}",
        "tips": [
            "Discover ops and arg keys via search_operations(query).",
            "Arg keys are case-sensitive and must match CyberChef exactly.",
            "Pass binary as Base64 text in input_data if needed.",
        ],
        "common_ops": [
            {"op": "From Base64", "args": {}},
            {"op": "To Base64", "args": {}},
            {"op": "From Hex", "args": {"Delimiter": "Auto"}},
            {"op": "Gunzip", "args": {}},
        ],
        "examples": examples,
    }


@mcp.tool()
def validate_recipe(req: ValidateRecipeIn) -> ValidateRecipeOut:
    """Validate a recipe array of {op, args}; suggest fixes and return a normalized form."""
    suggestions: List[SuggestionItem] = []
    errors: List[str] = []
    normalized: List[RecipeOp] = []

    for i, step in enumerate(req.recipe or []):
        name = (step.op or "").strip()
        if not name:
            errors.append(f"Step {i} missing 'op' name")
            continue
        if name not in CYBERCHEF_OPERATIONS:
            cands = [c[0] for c in process.extract(name, list(CYBERCHEF_OPERATIONS.keys()), limit=5)]
            suggestions.append(SuggestionItem(index=i, op=name, candidates=cands))
            continue
        expected = [a.get("name") for a in CYBERCHEF_OPERATIONS[name].get("args", [])]
        got = list((step.args or {}).keys())
        unexpected = [k for k in got if k not in expected]
        if unexpected:
            suggestions.append(SuggestionItem(index=i, op=name, missingArgs=None))  # add a new field if you want, e.g. unexpectedArgs
        missing = [k for k in expected if k not in got]
        if missing:
            suggestions.append(SuggestionItem(index=i, op=name, missingArgs=missing))
        normalized.append(step)

    ok = not suggestions and not errors
    return ValidateRecipeOut(ok=ok, errors=errors, suggestions=suggestions, normalized=normalized)


@mcp.tool()
def perform_magic_operation(
        input_data: str,
        depth: int = 3,
        intensive_mode: bool = False,
        extensive_language_support: bool = False,
        crib_str: str = "",
) -> dict:
    """
    Invoke CyberChef Magic; may be slow/approximate.

    :param input_data: the data in which to perform the magic operation on
    :param depth: how many levels of recursion to attempt pattern matching and speculative execution on the input data
    :param intensive_mode: optional argument which will run additional operations and take considerably longer to run
    :param extensive_language_support: if this is true all 245 languages are supported opposed to the top 38 by default
    :param crib_str: argument for any known plaintext string or regex
    :return:
    """
    request_data = {
        "input": input_data,
        "args": {
            "depth": depth,
            "intensive_mode": intensive_mode,
            "extensive_language_support": extensive_language_support,
            "crib": crib_str,
        },
    }
    result = create_api_request(endpoint="magic", request_data=request_data)
    return result


def main():
    """Initialize and run the server"""
    full_parser = argparse.ArgumentParser(description="CyberChef MCP Server")
    full_parser.add_argument("--api-url", dest="api_url", default=DEFAULT_API_URL,
                             help=f"CyberChef API base URL (default: {DEFAULT_API_URL})")
    full_parser.add_argument("--host", dest="host", default=DEFAULT_HOST,
                             help=f"Server bind host (default: {DEFAULT_HOST})")
    full_parser.add_argument("--port", dest="port", type=int, default=DEFAULT_PORT,
                             help=f"Server port (default: {DEFAULT_PORT})")
    args = full_parser.parse_args(sys.argv[1:])

    global CYBERCHEF_API_URL
    CYBERCHEF_API_URL = args.api_url

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
