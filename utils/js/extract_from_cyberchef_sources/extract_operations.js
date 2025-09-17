const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

// CLI arguments parsing for operationsDir and outputFile
function parseCliArgs(argv) {
  const args = { operationsDir: './src/core/operations', outputFile: 'operations.json' };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--operationsDir' && i + 1 < argv.length) {
      args.operationsDir = argv[++i];
    } else if (a === '--outputFile' && i + 1 < argv.length) {
      args.outputFile = argv[++i];
    }
  }
  // Resolve to absolute paths relative to current working directory
  args.operationsDir = path.resolve(process.cwd(), args.operationsDir);
  args.outputFile = path.resolve(process.cwd(), args.outputFile);
  return args;
}

const { operationsDir, outputFile } = parseCliArgs(process.argv);

// Parse named import statements to map identifiers to module paths
function parseImportMap(fileContent, filePath) {
  const dir = path.dirname(filePath);
  const importMap = new Map();
  const importRegex = /import\s*\{([^}]+)\}\s*from\s*["'](.+?)["'];/g;
  let match;
  while ((match = importRegex.exec(fileContent)) !== null) {
    const names = match[1].split(',').map(s => s.trim()).filter(Boolean);
    const rel = match[2];
    const abs = path.resolve(dir, rel);
    for (const n of names) {
      const [orig, alias] = n.split(/\s+as\s+/);
      const key = (alias || orig).trim();
      importMap.set(key, { absPath: abs, exportName: (orig || key).trim() });
    }
  }
  return importMap;
}

const moduleCache = new Map();
async function importModule(absPath) {
  if (moduleCache.has(absPath)) return moduleCache.get(absPath);
  const mod = await import(pathToFileURL(absPath).href);
  moduleCache.set(absPath, mod);
  return mod;
}

function extractExportConstArrayFromSource(absPath, exportName) {
  try {
    const src = fs.readFileSync(absPath, 'utf8');
    const re = new RegExp(`export\\s+const\\s+${exportName}\\s*=\\s*(\\[[\\s\\S]*?\\]);`);
    const m = src.match(re);
    if (m) {
      const arrStr = m[1];
      const prepared = replaceBareIdentifiersWithStrings(arrStr);
      const val = jsonishToValue(prepared);
      return Array.isArray(val) ? val : null;
    }
  } catch (e) {
    // ignore
  }
  return null;
}

// Extract a simple field value from constructor body
function extractField(content, fieldName) {
  const patterns = [
    new RegExp(`this\\.${fieldName}\\s*=\\s*["']([\\s\\S]*?)["'];`, 's'),
    new RegExp(`this\\.${fieldName}\\s*=\\s*["]([^"']*?)["];`),
    new RegExp(`this\\.${fieldName}\\s*=\\s*["]([^"']*?)["];`),
    new RegExp(`this\\.${fieldName}\\s*=\\s*(null);`)
  ];
  for (const pattern of patterns) {
    const match = content.match(pattern);
    if (match) return match[1] === 'null' ? null : match[1];
  }
  return null;
}

function findBalancedBlock(source, startIndex, openChar, closeChar) {
  let depth = 0;
  for (let i = startIndex; i < source.length; i++) {
    const ch = source[i];
    if (ch === openChar) depth++;
    else if (ch === closeChar) {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

function extractConstructorBody(fileContent) {
  const idx = fileContent.indexOf('constructor() {');
  if (idx === -1) return null;
  const start = idx + 'constructor() {'.length;
  const end = findBalancedBlock(fileContent, idx, '{', '}');
  if (end === -1) return null;
  return fileContent.substring(start, end - 1);
}

function extractArrayLiteralAfter(prefix, content) {
  const startIdx = content.indexOf(prefix);
  if (startIdx === -1) return null;
  const arrayStart = content.indexOf('[', startIdx);
  if (arrayStart === -1) return null;
  const arrayEnd = findBalancedBlock(content, arrayStart, '[', ']');
  if (arrayEnd === -1) return null;
  return content.substring(arrayStart, arrayEnd);
}

function replaceBareIdentifiersWithStrings(src) {
  // Replace bare ALLCAPS identifiers (constants) with string tokens so JSON parsing works,
  // but do NOT replace inside quotes.
  let out = '';
  let i = 0;
  let inStr = false;
  let quote = '';
  while (i < src.length) {
    const ch = src[i];
    if (inStr) {
      out += ch;
      if (ch === '\\') {
        if (i + 1 < src.length) { out += src[i + 1]; i += 2; continue; }
      } else if (ch === quote) {
        inStr = false;
      }
      i++;
      continue;
    }
    if (ch === '"' || ch === '\'') {
      inStr = true; quote = ch; out += ch; i++; continue;
    }
    if (/[A-Z_]/.test(ch)) {
      // capture identifier
      let j = i + 1;
      while (j < src.length && /[A-Z0-9_]/.test(src[j])) j++;
      const ident = src.slice(i, j);
      if (/^[A-Z_][A-Z0-9_]*$/.test(ident)) {
        out += '"' + ident + '"';
        i = j;
        continue;
      }
    }
    out += ch;
    i++;
  }
  return out;
}

function jsonishToValue(str) {
  // Make a best-effort conversion of JS-like structure into JSON
  let s = str;
  s = s.replace(/(\w+)\s*:/g, '"$1":');
  s = s.replace(/'([^']*)'/g, '"$1"');
  s = s.replace(/,(\s*[}\]])/g, '$1');
  try { return JSON.parse(s); } catch { return null; }
}

function parseArgsManual(argsStr) {
  // Fallback: very lenient, try to keep names and types/values
  const val = jsonishToValue(argsStr);
  return Array.isArray(val) ? val : [];
}

async function resolvePlaceholders(val, importMap) {
  if (Array.isArray(val)) return Promise.all(val.map(v => resolvePlaceholders(v, importMap)));
  if (val && typeof val === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(val)) out[k] = await resolvePlaceholders(v, importMap);
    return out;
  }
  if (typeof val === 'string') {
    // Identify bare constants like COMPRESSION_TYPE
    const isIdent = /^[A-Z_][A-Z0-9_]*$/.test(val);
    if (isIdent) {
      const lookup = importMap.get(val);
      if (lookup) {
        try {
          const mod = await importModule(lookup.absPath);
          const exported = mod[lookup.exportName];
          if (exported !== undefined) return exported;
        } catch (e) {
          // Fallback: parse export const from source without executing module
          const arr = extractExportConstArrayFromSource(lookup.absPath, lookup.exportName);
          if (arr !== null) return arr;
          console.warn(`Failed to import ${lookup.absPath} for ${lookup.exportName}: ${e.message}`);
          return val;
        }
      }
    }
  }
  return val;
}

function normalizeArgType(arg) {
  const out = { ...arg };
  const originalType = (out.type || '').toLowerCase();
  let t = originalType;
  // Special handling for toggleString/toggleByteArray => bytes with encodings
  if (['togglestring', 'togglebytearray', 'togglebytes'].includes(t)) {
    out.type = 'bytes';
    // Map toggleValues to encodings
    const encMap = { hex: 'hex', utf8: 'utf8', latin1: 'latin1', base64: 'base64', raw: 'utf8' };
    // Spec requires hint encodings only: hex, utf8, latin1, base64
    out.encodings = ["hex","utf8","latin1","base64"];
    delete out.toggleValues;
    t = 'bytes';
  }
  // Map many to: string, bytes, integer, number, boolean, enum
  if (['text', 'string', 'shortstring', 'longstring', 'regex', 'bigstring','binarystring','binaryshortstring'].includes(t)) out.type = 'string';
  else if (['bytearray', 'arraybuffer', 'bytes', 'byte_string'].includes(t)) { out.type = 'bytes'; out.encodings = out.encodings || ["hex","utf8","latin1","base64"]; }
  else if (['int', 'integer'].includes(t)) out.type = 'integer';
  else if (['number', 'float', 'double'].includes(t)) out.type = 'number';
  else if (['boolean', 'toggle', 'switch', 'checkbox'].includes(t)) out.type = 'boolean';
  else if (['option', 'select', 'enum', 'argselector', 'editableoption', 'editableoptionshort'].includes(t)) out.type = 'enum';

  // Enum/argSelector: only keep names as options
  if (originalType === 'argselector') {
    if (out.value && Array.isArray(out.value)) {
      out.options = out.value.map(v => typeof v === 'string' ? v : v && v.name).filter(Boolean);
    }
    delete out.value;
  } else if (out.type === 'enum') {
    if (Array.isArray(out.value)) {
      if (out.value.length && typeof out.value[0] === 'object' && out.value[0].name) {
        out.options = out.value.map(v => v.name);
      } else {
        out.options = out.value;
      }
      delete out.value;
    }
  }

  // Constraints normalization
  if (out.hasOwnProperty('defaultValue') && !out.hasOwnProperty('default')) {
    out.default = out.defaultValue; delete out.defaultValue;
  }
  if (out.required === undefined) out.required = false;

  // Prune fields not in the simplified schema
  for (const k of Object.keys(out)) {
    if (!['name','type','options','required','default','minLength','maxLength','pattern','length','min','max','encodings'].includes(k)) {
      // keep if constraint-like, else drop
      if (!['name','type','options','required','default','minLength','maxLength','pattern','length','min','max','encodings'].includes(k)) {
        if (!(['name','type'].includes(k))) delete out[k];
      }
    }
  }

  return out;
}

function normalizeChecks(checks) {
  return checks || [];
}

async function extractOperationMetadataAsync(fileContent, filePath, fileName) {
  const constructorBody = extractConstructorBody(fileContent);
  if (!constructorBody) return null;
  const importMap = parseImportMap(fileContent, filePath);
  const metadata = {
    name: extractField(constructorBody, 'name'),
    module: extractField(constructorBody, 'module'),
    description: extractField(constructorBody, 'description'),
    infoUrl: extractField(constructorBody, 'infoURL'),
    inputType: extractField(constructorBody, 'inputType'),
    outputType: extractField(constructorBody, 'outputType'),
    args: [],
    checks: []
  };

  const argsLit = extractArrayLiteralAfter('this.args =', constructorBody);
  if (argsLit) {
    const prepared = replaceBareIdentifiersWithStrings(argsLit);
    let args = jsonishToValue(prepared);
    if (!Array.isArray(args)) args = parseArgsManual(prepared);
    // Resolve placeholders/constants to inline values
    args = await resolvePlaceholders(args, importMap);
    metadata.args = args.map(a => normalizeArgType(a || {}));
  }

  const checksLit = extractArrayLiteralAfter('this.checks =', constructorBody);
  if (checksLit) {
    const preparedChecks = replaceBareIdentifiersWithStrings(checksLit);
    let checks = jsonishToValue(preparedChecks) || [];
    checks = await resolvePlaceholders(checks, importMap);
    metadata.checks = normalizeChecks(checks);
  }

  return metadata;
}

async function extractAllOperations() {
  const operations = {};
  const files = fs.readdirSync(operationsDir).filter(f => f.endsWith('.mjs'));
  for (const file of files) {
    const filePath = path.join(operationsDir, file);
    const fileContent = fs.readFileSync(filePath, 'utf8');
    const meta = await extractOperationMetadataAsync(fileContent, filePath, file);
    if (meta && meta.name) {
      const { name, ...rest } = meta;
      operations[name] = rest;
    }
  }
  fs.writeFileSync(outputFile, JSON.stringify(operations, null, 2));
}

if (require.main === module) {
  extractAllOperations().catch(err => { console.error(err); process.exit(1); });
}
