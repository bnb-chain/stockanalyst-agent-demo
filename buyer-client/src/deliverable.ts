import { getAddress, keccak256, toUtf8Bytes } from "ethers";
import { isLosslessNumber, parse } from "lossless-json";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

type JsonValue =
  | null
  | boolean
  | string
  | { value: string; isLosslessNumber: true }
  | JsonValue[]
  | { [key: string]: JsonValue };

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

function canonicalJson(value: JsonValue): string {
  if (value === null || typeof value === "boolean") return String(value);
  if (typeof value === "string") return jsonString(value);
  if (isLosslessNumber(value)) return value.value;
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const objectValue = value as { [key: string]: JsonValue };
  return `{${Object.keys(objectValue).sort().map(
    (key) => `${jsonString(key)}:${canonicalJson(objectValue[key]!)}`,
  ).join(",")}}`;
}

function object(value: JsonValue, field: string): Record<string, JsonValue> {
  if (value === null || Array.isArray(value) || typeof value !== "object" || isLosslessNumber(value)) {
    throw new Error(`DeliverableManifest.${field} must be an object`);
  }
  return value;
}

function integer(value: JsonValue, field: string): bigint {
  if (!isLosslessNumber(value) || !/^(0|[1-9][0-9]*)$/.test(value.value)) {
    throw new Error(`DeliverableManifest.${field} must be an unsigned integer`);
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
    parsed = parse(rawText) as JsonValue;
  } catch {
    throw new Error("DeliverableManifest is not valid JSON");
  }

  const manifest = object(parsed, "root");
  if (integer(manifest["version"]!, "version") !== 1n) {
    throw new Error("Unsupported DeliverableManifest version");
  }
  if (integer(manifest["job_id"]!, "job_id") !== expected.jobId) {
    throw new Error("DeliverableManifest job_id does not match the current job");
  }
  if (integer(manifest["chain_id"]!, "chain_id") !== expected.chainId) {
    throw new Error("DeliverableManifest chain_id does not match the current chain");
  }

  const contracts = object(manifest["contracts"]!, "contracts");
  for (const key of ["commerce", "router", "policy"] as const) {
    if (address(contracts[key]!, `contracts.${key}`) !== getAddress(expected.contracts[key])) {
      throw new Error(`DeliverableManifest contracts.${key} does not match configuration`);
    }
  }

  const response = object(manifest["response"]!, "response");
  const content = string(response["content"]!, "response.content");
  string(response["content_type"]!, "response.content_type");

  const actual = keccak256(toUtf8Bytes(canonicalJson(parsed)));
  if (actual.toLowerCase() !== expected.commitment.toLowerCase()) {
    throw new Error("DeliverableManifest commitment does not match the on-chain deliverable");
  }
  return content;
}
