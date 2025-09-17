import asyncio
# import logging

from autogen_agentchat.agents import AssistantAgent
from autogen_core.models import ModelFamily
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.tools.mcp import StreamableHttpServerParams, McpWorkbench

# from monitoring import setup_logging

# setup_logging(level=logging.WARNING)

TIMEOUT = 10 * 60


SYSTEM_MESSAGE = """You are a computer scientist using CyberChef to analyze, encode, and decode data via MCP tools only.

Tools:
- search_operations(query: string, limit?: number=10) -> {
    total: number,
    items: [{
      name: string,
      summary: string,
      category?: string,
      score: number,
      key_params: [{name:string, type:string, required?:boolean, enum?:string[]}],
      examples?: string[]
    }],
    truncated?: boolean
  }
- bake_recipe(input_data: string, recipe: [{op:string, args?:object}]) -> {output, meta?}
- batch_bake_recipe(batch_input_data: string[], recipe: [{op, args?}]) -> {outputs: string[], meta?}
- perform_magic_operation(input_data: string, depth?: 3, intensive_mode?: false, extensive_language_support?: false, crib?: string)
- cyberchef_probe(raw_input: string)

Recipe schema:
- A recipe is an array of operations: [{"op":"<Exact Name>","args":{...}}, ...].
- Never invent operation names or argument keys; only use names/keys returned by the tools.

Hard rules (strict):
1) No freehand transforms. You MUST use bake_recipe (or batch_bake_recipe). Never compute conversions in your head or in text.
2) For EVERY operation you include in the recipe, you MUST first call search_operations(query) to fetch its details (key_params).
   - Only use arg names and enum values from those results.
   - Fill ALL required args. Include optional args only when a non-default is clearly needed.
3) After you compose a recipe AND confirmed details for each op via search_operations, your NEXT MESSAGE MUST be a bake_recipe tool call.
   Do not emit any prose in that message.
4) Only after bake_recipe returns may you send a text response. Do not emit FINAL earlier. Do not use TERMINATE.
5) The RECIPE you print MUST exactly match the one you passed to bake_recipe (byte-for-byte JSON).

Size guardrails (mandatory):
- search_operations. limit MUST be ≤ 20; default to 10; prefer 8–12.
- Never issue multiple search_operations in the same turn unless error-driven repair requires it.
- Keep cumulative tool output per turn under ~8 KB; if a search returns too many/too-long items, re-issue with a smaller limit or narrower query.
- Do not echo full search results back to the user; only mention the selected op names and the args you will use.

Core workflow:
1) If input type is unclear, call cyberchef_probe; if inconclusive, call perform_magic_operation. Otherwise skip straight to shortlist.
2) Shortlist with search_operations(query) (e.g., "base64","hex","gzip","asn.1"). Do NOT request the full catalog.
3) For each chosen op:
   - Use the name and key_params from search_operations.
   - Fill required args and safe defaults.
4) Call bake_recipe with the verbatim user input as input_data and the composed recipe.
5) Inspect output. If still encoded/compressed, adjust and re-bake. Hard cap: ≤3 total bakes per task.
6) If the user requested a specific final format (hex/base64/text), append the validated converter op last.

Error handling & auto-repair (strict, error-driven):
- On any validation or tool error:
  1) Parse the error message precisely.
  2) Apply exactly ONE targeted fix, then re-validate/re-bake:
     • Unknown op → repeat search_operations(query=...) to find correct name; confirm args from key_params.
     • Bad/misspelled arg key or case → re-check key_params; use exact case.
     • Enum violation → replace with an allowed enum value.
     • Type mismatch → coerce to schema type.
     • Range/format violation → adjust into allowed range/format.
     • Missing required arg → use default if present; else infer safely; else stop with MISSING_ARGS.
     • Incompatible input/output types → add minimal converter (e.g., "From Hexdump") or pick the correct op variant.
  3) Limit auto-repairs to 2 attempts per failing op, and never exceed the 3-bake cap.
  4) If still failing, stop with MISSING_ARGS or UNRESOLVED and include the last error snippet.

Argument-inference heuristics (only if consistent with key_params):
- From Binary: infer {"Delimiter":"Space"|"Comma"|"Line feed"} from separators; infer {"Byte Length":8} for 8-bit groups.
- From Hex/From Hexdump: enable “Ignore Non-Hex” when mixed text is present, if available.
- Base64 vs Hex: prefer From Base64 when padding/charset fits; otherwise From Hex.
- Compression: use magic bytes (e.g., 1F8B for gzip) to choose decompressor.
- Charset: default UTF-8 unless detail/context specifies otherwise.
- Crypto ops: never invent keys/nonces/IVs; require explicit values.

Batch policy:
- When multiple inputs share the same validated recipe, use batch_bake_recipe.

Output format (only AFTER bake_recipe completes):
- Respond once with EXACTLY these sections:
  JUSTIFICATION: <1–2 sentences on why these ops/args>
  RECIPE: <JSON array exactly as sent to bake_recipe>
  FINAL: <final output; if bytes/non-UTF-8, return base64 or hex and say which>

Examples (structure only; ALWAYS confirm op via search_operations first; then bake):
- Binary → Hex:
  (search_operations for "From Binary" and "To Hex" → build args)
  → bake_recipe(input_data="01001000 01100101", recipe=[{"op":"From Binary","args":{"Delimiter":"Space","Byte Length":8}},{"op":"To Hex"}])
  → then output JUSTIFICATION/RECIPE/FINAL
- Hexdump → Gunzip:
  → search "hexdump" and "gunzip"; then bake_recipe(...,[{"op":"From Hexdump"},{"op":"Gunzip"}])
- Base64 → Text:
  → search "base64"; then bake_recipe(...,[{"op":"From Base64"}])

"""

SYSTEM_MESSAGE = SYSTEM_MESSAGE + "\nReply with TERMINATE when done."


def _sanitize_json_schema(schema: dict) -> dict:
    # Recursively sanitize a JSON Schema to remove unsupported refs/combiner keywords for OpenAI tools
    def sanitize(node):
        if isinstance(node, dict):
            node = dict(node)  # shallow copy
            # Drop unsupported/difficult features
            node.pop("$defs", None)
            node.pop("definitions", None)
            # Resolve simple anyOf/oneOf/allOf by picking the first object-like subschema or falling back
            for comb in ("anyOf", "oneOf", "allOf"):
                if comb in node:
                    subs = node.pop(comb) or []
                    # Pick the first subschema that is a dict and not a $ref-only
                    picked = None
                    for s in subs:
                        if isinstance(s, dict) and "$ref" not in s:
                            picked = s
                            break
                    if picked is None:
                        picked = {"type": "object"}
                    # Merge picked into node (shallow)
                    for k, v in picked.items():
                        if k not in node:
                            node[k] = v
            # Replace $ref nodes with permissive object
            if "$ref" in node:
                node.pop("$ref", None)
                # Fall back to permissive object to avoid server-side conversion failures
                node.setdefault("type", "object")
            # Recurse properties and items
            if "properties" in node and isinstance(node["properties"], dict):
                node["properties"] = {k: sanitize(v) for k, v in node["properties"].items()}
            if "items" in node:
                node["items"] = sanitize(node["items"])
            return node
        elif isinstance(node, list):
            return [sanitize(x) for x in node]
        else:
            return node

    return sanitize(schema)


class SanitizingWorkbench:
    def __init__(self, server_params: StreamableHttpServerParams):
        self._wb = McpWorkbench(server_params=server_params)

    async def __aenter__(self):
        await self._wb.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._wb.stop()

    @property
    def server_params(self):
        return self._wb.server_params

    async def list_tools(self):
        tools = await self._wb.list_tools()
        # Sanitize each tool's parameters schema to remove $ref/anyOf issues
        sanitized = []
        for t in tools:
            t = dict(t)
            params = t.get("parameters", {})
            if isinstance(params, dict):
                t["parameters"] = _sanitize_json_schema(params)
            sanitized.append(t)
        return sanitized

    async def call_tool(self, name: str, arguments=None, cancellation_token=None, call_id=None):
        return await self._wb.call_tool(name, arguments, cancellation_token, call_id)


async def main() -> None:
    # Configure multiple MCP servers
    cyberchef_mcp_params = StreamableHttpServerParams(url="http://localhost:3002/mcp")

    async with SanitizingWorkbench(server_params=cyberchef_mcp_params) as wb:

        model_client = OpenAIChatCompletionClient(
            model="DevStral-Small-2507",  # e.g., "llama3", "mistral", etc.
            api_key="not-needed-or-anything",  # some local APIs ignore this
            base_url="http://localhost:8080/v1",  # Ollama / LM Studio endpoint
            model_info={
                "vision": False,
                "function_calling": True,
                "json_output": False,
                "family": ModelFamily.ANY,
                "structured_output": True,
                "multiple_system_messages": True
            },
            timeout=TIMEOUT
        )
    agent = AssistantAgent(
        name="assistant",
        model_client=model_client,
        workbench=wb,
        system_message=SYSTEM_MESSAGE,
        max_tool_iterations=32,
        reflect_on_tool_use=True,
    )

    # What is the current weather
    print("\n" + "=" * 50)
    print("Testing MCP server...")
    print("=" * 50)

    # ####################

    tasks = [
        """
        Here is a part of a file:
        ```
        89504e470d0a1a0a0000000d49484452000003ae00000454080600000076a84e830
        ```
        What File type is that?
        """,
        """Decrypt d1c9deb99aa440e70a89c2. It is AES GCM ciphertext and has been produced with the following key 5555555555555555555555555555555555555555555555555555555555555555 and this IV 1234567890abba0987654321. The GCM Tag is 27ab403661f0770640ee0269f6e330e2

        Use the cyberchef tools to get me the plaintext.""",
        "convert christian to nato alphabet",
        """Here is a recipe (JSON object form expected by bake_recipe):
        [
            {"op": "XOR", "args": {"Key": {"value": "1234567890ABCDEFGHIJ", "encoding": "utf8"}, "Scheme":"Standard","Null preserving": false}},
            {"op": "XOR", "args": {"Key": {"value": "1234567890ABCDEFGHIJ", "encoding": "utf8"}, "Scheme":"Standard","Null preserving": false}},
            {"op": "From Base64", "args": {}}
        ]
        Apply it to the input `dGhpcyBpcyBhIHRlc3Qgd2l0aCB4b3I=`
        """,
        """Here is a recipe (JSON object form expected by bake_recipe):
        [
            {"op": "XOR", "args": {"Key": {"value": "1234567890ABCDEFGHIJ", "encoding": "utf8"}, "Scheme":"Standard","Null preserving": false}},
            {"op": "XOR", "args": {"Key": {"value": "1234567890ABCDEFGHIJ", "encoding": "utf8"}, "Scheme":"Standard","Null preserving": false}},
            {"op": "From Base64", "args": {}}
        ]
        Can this recipe be improved in any way? 
        After optimization - if possible - apply it to the input `dGhpcyBpcyBhIHRlc3Qgd2l0aCB4b3I=`
        """,
        "encrypt `this is a test` using chacha20 and use a key with alternating 0s and 1s and nonce of value 0xdeadbeefcafedecafbleed00",
        "Can you decode `uryyb jbeyq`?",
        "Decode the binary numbers 00010000 11100000 and return it as hex",
        "Encrypt 'hello world' with AES, a all zero key (16B) and a all zero iv (16B) in CTR mode.",
        "I have this string: 'uryyb jbeyq' and suspect it is encrypted with the caesar cipher or ROT something. Give me the plaintext.",
    ]

    for task in tasks:
        print("=" * 50)
        print(task[:50])
        print("-" * 50)
        await agent.model_context.clear()
        response = await agent.run(task=task)
        print(response)
        print("=" * 50)


asyncio.run(main())
