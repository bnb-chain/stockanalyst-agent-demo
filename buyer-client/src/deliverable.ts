import { getAddress, keccak256, toUtf8Bytes } from "ethers";
import { LosslessNumber, parse } from "lossless-json";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

const MAX_INTEGER_DIGITS = 4_300;
const MAX_INTEGER_DIGITS_LABEL = "4,300";
const MAX_JSON_NESTING_DEPTH = 128;
const JSON_NESTING_DEPTH_ERROR =
  "DeliverableManifest exceeds the maximum JSON nesting depth";

type JsonValue =
  | null
  | boolean
  | string
  | LosslessNumber
  | PythonNonFinite
  | JsonValue[]
  | { [key: string]: JsonValue };

class PythonNonFinite {
  constructor(readonly value: "NaN" | "Infinity" | "-Infinity") {}
}

export interface DeliverableExpectation {
  jobId: bigint;
  chainId: bigint;
  contracts: {
    commerce: string;
    router: string;
    policy: string;
  };
  commitment: string;
}

interface JsonStringToken {
  start: number;
  end: number;
  value: string;
  isObjectKey: boolean;
}

type JsonContainerScope =
  | { kind: "object"; keys: Set<string> }
  | { kind: "array" };

class JsonNestingDepthError extends Error {
  constructor() {
    super(JSON_NESTING_DEPTH_ERROR);
    this.name = "JsonNestingDepthError";
  }
}

function isNumberNode(value: unknown): value is LosslessNumber {
  return value instanceof LosslessNumber;
}

function isPythonNonFinite(value: unknown): value is PythonNonFinite {
  return value instanceof PythonNonFinite;
}

function scanJsonStrings(rawText: string): JsonStringToken[] {
  const tokens: JsonStringToken[] = [];
  let index = 0;
  while (index < rawText.length) {
    if (rawText[index] !== '"') {
      index += 1;
      continue;
    }

    const start = index;
    index += 1;
    while (index < rawText.length && rawText[index] !== '"') {
      index += rawText[index] === "\\" ? 2 : 1;
    }
    if (index >= rawText.length) break;

    const end = index + 1;
    const value = JSON.parse(rawText.slice(start, end)) as string;
    let next = end;
    while (next < rawText.length && /\s/.test(rawText[next]!)) next += 1;
    tokens.push({ start, end, value, isObjectKey: rawText[next] === ":" });
    index = end;
  }
  return tokens;
}

function rejectDuplicateObjectKeys(
  rawText: string,
  stringTokens: readonly JsonStringToken[],
): void {
  const scopes: JsonContainerScope[] = [];
  let tokenIndex = 0;
  let index = 0;

  while (index < rawText.length) {
    const token = stringTokens[tokenIndex];
    if (token?.start === index) {
      const scope = scopes[scopes.length - 1];
      if (token.isObjectKey && scope?.kind === "object") {
        if (scope.keys.has(token.value)) {
          throw new SyntaxError("Duplicate object key");
        }
        scope.keys.add(token.value);
      }
      index = token.end;
      tokenIndex += 1;
      continue;
    }

    if (rawText[index] === "{" || rawText[index] === "[") {
      if (scopes.length >= MAX_JSON_NESTING_DEPTH) {
        throw new JsonNestingDepthError();
      }
      if (rawText[index] === "{") {
        scopes.push({ kind: "object", keys: new Set<string>() });
      } else {
        scopes.push({ kind: "array" });
      }
    } else if (
      (rawText[index] === "}" && scopes[scopes.length - 1]?.kind === "object")
      || (rawText[index] === "]" && scopes[scopes.length - 1]?.kind === "array")
    ) {
      scopes.pop();
    }
    index += 1;
  }
}

function protectPrototypeKeys(
  rawText: string,
  tokens: readonly JsonStringToken[],
): {
  protectedText: string;
  sentinel: string;
} {
  const strings = new Set(tokens.map(({ value }) => value));
  let sentinel = "\u0000__proto__";
  while (strings.has(sentinel)) sentinel += "\u0000";

  let protectedText = "";
  let previousEnd = 0;
  for (const token of tokens) {
    if (!token.isObjectKey || token.value !== "__proto__") continue;
    protectedText += rawText.slice(previousEnd, token.start);
    protectedText += JSON.stringify(sentinel);
    previousEnd = token.end;
  }
  protectedText += rawText.slice(previousEnd);
  return { protectedText, sentinel };
}

function protectPythonNonFinite(
  rawText: string,
  tokens: readonly JsonStringToken[],
): {
  protectedText: string;
  sentinels: ReadonlyMap<string, PythonNonFinite>;
} {
  const strings = new Set(tokens.map(({ value }) => value));
  const sentinels = new Map<string, PythonNonFinite>();
  for (const value of ["NaN", "Infinity", "-Infinity"] as const) {
    let sentinel = `\u0000python-non-finite:${value}`;
    while (strings.has(sentinel)) sentinel += "\u0000";
    strings.add(sentinel);
    sentinels.set(sentinel, new PythonNonFinite(value));
  }

  let protectedText = "";
  let index = 0;
  let tokenIndex = 0;
  while (index < rawText.length) {
    const token = tokens[tokenIndex];
    if (token?.start === index) {
      protectedText += rawText.slice(token.start, token.end);
      index = token.end;
      tokenIndex += 1;
      continue;
    }

    let matched: "NaN" | "Infinity" | "-Infinity" | undefined;
    for (const candidate of ["-Infinity", "Infinity", "NaN"] as const) {
      if (rawText.startsWith(candidate, index)) {
        matched = candidate;
        break;
      }
    }
    if (matched) {
      const before = index === 0 ? "" : rawText[index - 1]!;
      const after = rawText[index + matched.length] ?? "";
      const validBefore = index === 0 || /[\s\[{: ,]/.test(before);
      const validAfter = index + matched.length === rawText.length || /[\s,\]}]/.test(after);
      if (validBefore && validAfter) {
        const sentinel = [...sentinels.entries()]
          .find(([, marker]) => marker.value === matched)![0];
        protectedText += JSON.stringify(sentinel);
        index += matched.length;
        continue;
      }
    }
    protectedText += rawText[index];
    index += 1;
  }
  return { protectedText, sentinels };
}

function nullPrototypeRecords(
  value: unknown,
  prototypeKeySentinel: string,
  nonFiniteSentinels: ReadonlyMap<string, PythonNonFinite>,
): JsonValue {
  if (typeof value === "string") {
    return nonFiniteSentinels.get(value) ?? value;
  }
  if (
    value === null
    || typeof value === "boolean"
    || isNumberNode(value)
  ) {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => nullPrototypeRecords(
      item,
      prototypeKeySentinel,
      nonFiniteSentinels,
    ));
  }
  if (typeof value !== "object") {
    throw new Error("unsupported JSON value");
  }

  const record = Object.create(null) as Record<string, JsonValue>;
  for (const key of Object.keys(value)) {
    const ownKey = key === prototypeKeySentinel ? "__proto__" : key;
    record[ownKey] = nullPrototypeRecords(
      (value as Record<string, unknown>)[key],
      prototypeKeySentinel,
      nonFiniteSentinels,
    );
  }
  return record;
}

function parseJson(rawText: string): JsonValue {
  const stringTokens = scanJsonStrings(rawText);
  rejectDuplicateObjectKeys(rawText, stringTokens);
  const { protectedText, sentinel } = protectPrototypeKeys(rawText, stringTokens);
  const protectedTokens = scanJsonStrings(protectedText);
  const nonFinite = protectPythonNonFinite(protectedText, protectedTokens);
  return nullPrototypeRecords(
    parse(nonFinite.protectedText),
    sentinel,
    nonFinite.sentinels,
  );
}

function jsonString(value: string): string {
  return JSON.stringify(value).replace(
    /[^\u0020-\u007e]/gu,
    (character) => [...character]
      .map((unit) => {
        const point = unit.codePointAt(0)!;
        if (point <= 0xffff) return `\\u${point.toString(16).padStart(4, "0")}`;
        const shifted = point - 0x10000;
        const high = 0xd800 + (shifted >> 10);
        const low = 0xdc00 + (shifted & 0x3ff);
        return `\\u${high.toString(16)}\\u${low.toString(16)}`;
      })
      .join(""),
  );
}

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = [...left];
  const rightPoints = [...right];
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    const difference = leftPoints[index]!.codePointAt(0)! - rightPoints[index]!.codePointAt(0)!;
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function pythonFloat(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Number.POSITIVE_INFINITY) return "Infinity";
  if (value === Number.NEGATIVE_INFINITY) return "-Infinity";
  if (Object.is(value, -0)) return "-0.0";
  if (value === 0) return "0.0";

  const sign = value < 0 ? "-" : "";
  const [coefficient, exponentText] = Math.abs(value).toExponential().split("e");
  const exponent = Number(exponentText);
  const digits = coefficient!.replace(".", "");

  // CPython repr/json use fixed notation for decimal exponents -4 through 15.
  if (exponent >= -4 && exponent < 16) {
    const point = exponent + 1;
    if (point <= 0) {
      return `${sign}0.${"0".repeat(-point)}${digits}`;
    }
    if (point >= digits.length) {
      return `${sign}${digits}${"0".repeat(point - digits.length)}.0`;
    }
    return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
  }

  const fraction = digits.length > 1 ? `.${digits.slice(1)}` : "";
  const exponentSign = exponent >= 0 ? "+" : "-";
  return `${sign}${digits[0]}${fraction}e${exponentSign}${String(
    Math.abs(exponent),
  ).padStart(2, "0")}`;
}

function canonicalNumber(value: LosslessNumber): string {
  if (/^-?(?:0|[1-9][0-9]*)$/.test(value.value)) {
    const digitCount = value.value.startsWith("-")
      ? value.value.length - 1
      : value.value.length;
    if (digitCount > MAX_INTEGER_DIGITS) {
      throw new Error(
        `DeliverableManifest integer exceeds the ${MAX_INTEGER_DIGITS_LABEL}-digit limit`,
      );
    }
    return value.value === "-0" ? "0" : value.value;
  }
  return pythonFloat(Number(value.value));
}

function canonicalJson(value: JsonValue): string {
  if (value === null || typeof value === "boolean") return String(value);
  if (typeof value === "string") return jsonString(value);
  if (isNumberNode(value)) return canonicalNumber(value);
  if (isPythonNonFinite(value)) return value.value;
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const objectValue = value as { [key: string]: JsonValue };
  return `{${Object.keys(objectValue).sort(compareUnicodeCodePoints).map(
    (key) => `${jsonString(key)}:${canonicalJson(objectValue[key]!)}`,
  ).join(",")}}`;
}

function object(value: JsonValue, field: string): Record<string, JsonValue> {
  if (
    value === null
    || Array.isArray(value)
    || typeof value !== "object"
    || isNumberNode(value)
    || isPythonNonFinite(value)
  ) {
    throw new Error(`DeliverableManifest.${field} must be an object`);
  }
  return value;
}

function required(
  value: Record<string, JsonValue>,
  key: string,
  field: string,
): JsonValue {
  if (!Object.prototype.hasOwnProperty.call(value, key)) {
    throw new Error(`DeliverableManifest.${field} is required`);
  }
  return value[key]!;
}

function integer(value: JsonValue, field: string): bigint {
  if (!isNumberNode(value) || !/^(0|[1-9][0-9]*)$/.test(value.value)) {
    throw new Error(`DeliverableManifest.${field} must be an unsigned integer`);
  }
  if (value.value.length > MAX_INTEGER_DIGITS) {
    throw new Error(
      `DeliverableManifest.${field} integer exceeds the ${MAX_INTEGER_DIGITS_LABEL}-digit limit`,
    );
  }
  return BigInt(value.value);
}

function string(value: JsonValue, field: string): string {
  if (typeof value !== "string") {
    throw new Error(`DeliverableManifest.${field} must be a string`);
  }
  return value;
}

function address(value: JsonValue, field: string): string {
  try {
    return getAddress(string(value, field));
  } catch {
    throw new Error(`DeliverableManifest.${field} must be an EVM address`);
  }
}

export function verifyDeliverableManifest(
  rawText: string,
  expected: DeliverableExpectation,
): string {
  if (Buffer.byteLength(rawText, "utf8") > MAX_PAYLOAD_BYTES) {
    throw new Error("DeliverableManifest size exceeds the 2 MiB limit");
  }

  let parsed: JsonValue;
  try {
    parsed = parseJson(rawText);
  } catch (error) {
    if (error instanceof JsonNestingDepthError) {
      throw new Error(JSON_NESTING_DEPTH_ERROR);
    }
    throw new Error("DeliverableManifest is not valid JSON");
  }

  const manifest = object(parsed, "root");
  if (integer(required(manifest, "version", "version"), "version") !== 1n) {
    throw new Error("Unsupported DeliverableManifest version");
  }
  if (integer(required(manifest, "job_id", "job_id"), "job_id") !== expected.jobId) {
    throw new Error("DeliverableManifest job_id does not match the current job");
  }
  if (integer(required(manifest, "chain_id", "chain_id"), "chain_id") !== expected.chainId) {
    throw new Error("DeliverableManifest chain_id does not match the current chain");
  }

  const contracts = object(required(manifest, "contracts", "contracts"), "contracts");
  for (const key of ["commerce", "router", "policy"] as const) {
    if (
      address(
        required(contracts, key, `contracts.${key}`),
        `contracts.${key}`,
      ) !== getAddress(expected.contracts[key])
    ) {
      throw new Error(`DeliverableManifest contracts.${key} does not match configuration`);
    }
  }

  const response = object(required(manifest, "response", "response"), "response");
  const content = string(
    required(response, "content", "response.content"),
    "response.content",
  );
  string(
    required(response, "content_type", "response.content_type"),
    "response.content_type",
  );

  const actual = keccak256(toUtf8Bytes(canonicalJson(parsed)));
  if (actual.toLowerCase() !== expected.commitment.toLowerCase()) {
    throw new Error("DeliverableManifest commitment does not match the on-chain deliverable");
  }
  return content;
}
