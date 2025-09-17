# Automatic extraction of CyberChef operations from CyberChef source code

This script extracts all operation definitions from the CyberChef source tree and produces a single `operations.json` file
that can be consumed by the MCP service in this repository.

Use this only if new operations are added to CyberChef. Or wait until this repo gets updated or create a PR.

## Prerequisites
- Node.js 18+ (required for dynamic `import()` of ES modules)
- A local checkout of the CyberChef sources from https://github.com/gchq/CyberChef (you only need the `src/core/operations` folder)

## Usage
You can run the extractor with defaults or by specifying paths explicitly via CLI options.

- Default behavior (relative to current working directory):
  - `operationsDir`: `./src/core/operations`
  - `outputFile`: `./operations.json`

Examples:


1. If you are inside a CyberChef checkout and want to write to the current dir:
   - `node utils/js/extract_from_cyberchef_sources/extract_operations.js`
2. From this repo, pointing to a sibling CyberChef clone and writing into this repo:
   - `node utils/js/extract_from_cyberchef_sources/extract_operations.js --operationsDir ../CyberChef/src/core/operations --outputFile utils/js/operations.json`
3. Using absolute paths:
   - `node utils/js/extract_from_cyberchef_sources/extract_operations.js --operationsDir /path/to/CyberChef/src/core/operations --outputFile /path/to/output/operations.json`

Notes:
- Both `--operationsDir` and `--outputFile` are resolved relative to your current working directory if you pass relative paths.
- The output file will be overwritten if it already exists.

## What is generated?
A JSON file mapping each CyberChef operation name to a simplified metadata object with its module, description, input/output types, arguments and checks. This file is used by the MCP service at runtime to know what tools/operations are available and how to call them.

## Troubleshooting
- Ensure you are pointing `--operationsDir` to CyberChef’s `src/core/operations` directory (containing multiple `.mjs` files).
- If Node.js throws module import errors, check your Node version (v18+ recommended).
- If nothing is generated, re-check the provided paths and your current working directory.