# Cyberchef MCP Server

Pydantic-powered MCP server exposing most CyberChef operations as structured tools.

- Sources for operation metadata: see [extract_operations.js](utils/js/extract_from_cyberchef_sources/extract_operations.js)
- Operations catalog JSON: utils/js/operations.json

## What is this?
This project wraps the CyberChef-server HTTP API in an MCP (Model Context Protocol) server so AI agents and MCP-aware apps can:
- Discover CyberChef operations with fuzzy search
- Inspect the exact argument schema for any operation
- Execute single- or multi-step CyberChef recipes against text or binary data
- Validate and repair recipes programmatically

## Prerequisites
You need a running CyberChef-server (the upstream API that performs the actual transforms):

```
git clone https://github.com/gchq/CyberChef-server
cd CyberChef-server
docker build -t cyberchef-server .
docker run -d --name=cyberchef-server -p 3000:3000 cyberchef-server
```

By default this MCP server talks to http://localhost:3000/; you can override with --api-url.

## Install (local)
```
# From the project root
python -m venv .venv && source .venv/bin/activate  # or use your preferred env manager
pip install -r requirements.txt
```

## Run (local)
```
python mcp_cyberchef_service.py \
  --api-url http://localhost:3000/ \
  --host 127.0.0.1 \
  --port 3002
```
This will start the MCP server using the streamable-http transport on the host/port you provide.

CLI flags:
- --api-url: Base URL of the upstream CyberChef-server (default http://localhost:3000/)
- --host: Interface to bind for the MCP server (default 127.0.0.1)
- --port: Port for the MCP server (default 3002)

## Run with Docker
Builds a lightweight image and starts the MCP server on port 3002.

From this directory:

```
docker build -f Dockerfile -t cyberchef-mcp .
```

Then run it (pointing to your CyberChef-server):

```
docker run -d -p 3002:3002 \
  cyberchef-mcp \
  --api-url http://host.docker.internal:3000/ \
  --host 0.0.0.0 \
  --port 3002
```

## MCP Tools exposed
These are the primary tools exported by the MCP server. Argument and return schemas are enforced with Pydantic models.

- search_operations(query: string, limit?: number=10, include_args?: boolean=false) → { total, items[], truncated? }
  Find relevant CyberChef operations by name/description with fuzzy matching. Optionally include argument lists.
- get_operation_args(op: string, compact?: boolean=true) → { ok, op, args[], error? }
  Return the exact argument schema for one operation; with compact=true, enum values are slugified.
- bake_recipe(input_data: string, recipe: [{op:string, args?:object}]) → { ok, output?, type?, errors[], warnings[] }
  Execute a single recipe for one input string.
- batch_bake_recipe(batch_input_data: string[], recipe: [...]) → { results: BakeRecipeResponse[] }
  Execute the same recipe for many inputs.
- validate_recipe(recipe: [{op, args?}]) → { ok, errors[], suggestions[], normalized? }
  Validate step names/args and suggest fixes or missing args.
- help_bake_recipe() → Cheat sheet with schema notes and examples for composing recipes.
- cyberchef_probe(raw_input: string) → ProbeOut
  Quick heuristics to guess encodings and propose a minimal recipe.
- perform_magic_operation(input_data: string, depth?: int=3, intensive_mode?: bool=false, extensive_language_support?: bool=false, crib_str?: string="") → dict
  Invoke CyberChef Magic; may be slow/approximate.

Tip: Operation names and argument keys are case-sensitive and must match CyberChef exactly. Use search_operations/get_operation_args first.

## Example (agent integration)
See example/test-cyberchef.py for a full integration with Microsoft Autogen MCP workbench. It spins up this server and drives it strictly via tools. A minimal flow:

1) search_operations("base64") to shortlist "From Base64".
2) bake_recipe with:
   - input_data: "SGVsbG8gV29ybGQh"
   - recipe: [{"op":"From Base64","args":{}}]

## Cross platform builds
Only once: `docker buildx create --use --name xbuilder2`

Further updates:
```
docker buildx use xbuilder2
docker buildx build --platform linux/amd64 -f mcp_servers/mcp_cyberchef/Dockerfile -t cyberchef-mcp-amd64 --load .
docker buildx use default
```

## Troubleshooting
- Connection errors when baking recipes: ensure CyberChef-server is running and --api-url points to it.
- Unknown op / bad args: call get_operation_args(op) and confirm exact key names and allowed enum values.
- Large search results: lower limit or narrow your query; the server truncates responses to stay under size caps.

