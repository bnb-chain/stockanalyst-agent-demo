/**
 * A2A JSON-RPC negotiate with the stock analysis agent.
 *
 * Supports both local (no auth) and platform (OAuth2 client_credentials) endpoints.
 * Set AGENT_CLIENT_ID + AGENT_CLIENT_SECRET in .env for platform deploys; leave
 * them unset for local dev (the token fetch is skipped automatically).
 */

import { randomUUID } from "node:crypto";
import { formatUnits, parseUnits, type Signer } from "ethers";
import {
  buildNotifyContext,
  createNotifyAuthorization,
  type NotifyOptions,
} from "./notify-auth.js";

export type { NotifyOptions } from "./notify-auth.js";

/** Default per-job spend ceiling (in U) when MAX_PRICE_U is unset. */
export const DEFAULT_MAX_PRICE_U = 100;

/**
 * Resolve the buyer's per-job spend ceiling in raw wei from `MAX_PRICE_U`.
 * The seller signs and returns its own price; without a client-side ceiling a
 * hostile or misconfigured seller could quote an arbitrarily large amount that
 * the buyer would fund up to its wallet balance. Fund nothing above this cap.
 */
export function resolveMaxBudgetWei(
  env: Readonly<Record<string, string | undefined>> = process.env,
): bigint {
  const raw = env["MAX_PRICE_U"];
  const maxU = raw !== undefined && raw.trim() !== "" ? Number(raw) : DEFAULT_MAX_PRICE_U;
  if (!Number.isFinite(maxU) || maxU <= 0) {
    throw new Error("MAX_PRICE_U must be a positive number");
  }
  return parseUnits(String(maxU), 18);
}

/**
 * Reject a seller quote that is non-positive or exceeds the buyer's spend cap,
 * BEFORE any on-chain funding. Comparison is exact (raw wei), never via float.
 */
export function assertQuoteWithinBudget(priceWei: bigint, maxBudgetWei: bigint): void {
  if (priceWei <= 0n) {
    throw new Error(`Seller quoted a non-positive price (${priceWei} wei); refusing to fund`);
  }
  if (priceWei > maxBudgetWei) {
    throw new Error(
      `Seller quote ${formatUnits(priceWei, 18)} U exceeds the MAX_PRICE_U cap of ` +
        `${formatUnits(maxBudgetWei, 18)} U; refusing to fund. ` +
        `Raise MAX_PRICE_U to accept a higher quote.`,
    );
  }
}

export const NOTIFY_CONTEXT_REQUIRED = "uomp_notify_context_required_v1";
const INVALID_NOTIFY_CONTEXT_MARKER =
  "Invalid negotiation: required notify-context marker missing or altered";

export interface NegotiationEnvelope {
  request: {
    task_description: string;
    terms: {
      deliverables: string;
      quality_standards: string;
      success_criteria?: string;
    };
  };
  response: {
    accepted: boolean;
    terms: {
      price: string;
      currency: string;
      deliverables?: string;
      quality_standards?: string;
      success_criteria?: string;
      [key: string]: unknown;
    };
    negotiated_at?: number;
    quote_expires_at?: number;
    estimated_completion_seconds?: number;
    negotiation_hash?: string;
    provider_sig?: string;
    reason?: string;
  };
  negotiated_at?: number;
  negotiation_hash?: string;
  provider_sig?: string;
  chain_id?: number;
  verifying_contract?: string;
}

// ── OAuth2 client_credentials token cache ────────────────────────────────────

let _cachedToken: {
  value: string;
  expiresAt: number;
  clientId: string;
  clientSecret: string;
  tokenUrl: string;
  scope: string;
} | null = null;

/**
 * Return `{ Authorization: "Bearer …" }` when AGENT_CLIENT_ID/SECRET are set.
 * Derives the token URL and scope from the A2A endpoint URL unless their
 * explicit AGENT_TOKEN_URL/AGENT_OAUTH_SCOPE overrides are configured.
 * Returns {} for local endpoints (no auth required).
 */
async function authHeaders(endpoint: string): Promise<Record<string, string>> {
  const clientId     = process.env["AGENT_CLIENT_ID"]     ?? "";
  const clientSecret = process.env["AGENT_CLIENT_SECRET"] ?? "";
  if (!clientId || !clientSecret) return {};

  const now = Date.now();
  // Token URL: same origin as the A2A endpoint, path /v1/oauth/token
  const origin   = new URL(endpoint).origin;
  const tokenUrl = process.env["AGENT_TOKEN_URL"] ?? `${origin}/v1/oauth/token`;

  // Scope: "invoke:<agentId>" extracted from /rt/<agentId>/ in the endpoint path
  const agentId = endpoint.match(/\/rt\/([^/]+)\//)?.[1] ?? "";
  const scope   = process.env["AGENT_OAUTH_SCOPE"] ?? (agentId ? `invoke:${agentId}` : "");

  if (
    _cachedToken &&
    _cachedToken.expiresAt > now + 30_000 &&
    _cachedToken.clientId === clientId &&
    _cachedToken.clientSecret === clientSecret &&
    _cachedToken.tokenUrl === tokenUrl &&
    _cachedToken.scope === scope
  ) {
    return { Authorization: `Bearer ${_cachedToken.value}` };
  }

  const res = await fetch(tokenUrl, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type:    "client_credentials",
      client_id:     clientId,
      client_secret: clientSecret,
      ...(scope ? { scope } : {}),
    }).toString(),
  });

  if (!res.ok) {
    throw new Error(`OAuth token error ${res.status}: ${await res.text()}`);
  }

  const data = await res.json() as { access_token: string; expires_in?: number };
  _cachedToken = {
    value:     data.access_token,
    expiresAt: now + ((data.expires_in ?? 3600) * 1000),
    clientId,
    clientSecret,
    tokenUrl,
    scope,
  };
  return { Authorization: `Bearer ${_cachedToken.value}` };
}

const AGENTCORE_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id";
const MIN_AGENTCORE_SESSION_ID_LENGTH = 33;
let _agentCoreSessionId: string | null = null;

function isAgentCoreEndpoint(endpoint: string): boolean {
  try {
    return /^bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com$/i.test(
      new URL(endpoint).hostname,
    );
  } catch {
    return false;
  }
}

function agentCoreSessionHeaders(endpoint: string): Record<string, string> {
  if (!isAgentCoreEndpoint(endpoint)) return {};

  const configuredSessionId = process.env["AGENT_SESSION_ID"];
  if (
    configuredSessionId !== undefined &&
    configuredSessionId.length < MIN_AGENTCORE_SESSION_ID_LENGTH
  ) {
    throw new Error(
      `AGENT_SESSION_ID must be at least ${MIN_AGENTCORE_SESSION_ID_LENGTH} characters for AWS AgentCore`,
    );
  }

  const sessionId = configuredSessionId ?? (_agentCoreSessionId ??= randomUUID());
  return { [AGENTCORE_SESSION_HEADER]: sessionId };
}

/** Normalise the A2A endpoint: strip trailing slash so both local and platform
 *  URLs work the same way.  Local: http://localhost:9000  → POST /
 *  Platform: https://…bnbchain.world/v1/rt/…/a2a         → POST /a2a   */
function a2aUrl(endpoint: string): string {
  return endpoint.replace(/\/$/, "");
}

// ── A2A calls ────────────────────────────────────────────────────────────────

export async function negotiate(
  endpoint: string,
  task: string,
  deliverables: string,
  quality: string
): Promise<NegotiationEnvelope> {
  const sessionHeaders = agentCoreSessionHeaders(endpoint);
  const payload = {
    jsonrpc: "2.0",
    id: 1,
    method: "message/send",
    params: {
      message: {
        role: "user",
        messageId: `negotiate-${Date.now()}`,
        parts: [
          {
            kind: "data",
            data: {
              skill: "negotiate",
              task_description: task,
              terms: {
                deliverables,
                quality_standards: quality,
                success_criteria: NOTIFY_CONTEXT_REQUIRED,
              },
            },
          },
        ],
      },
    },
  };

  const res = await fetch(a2aUrl(endpoint), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...sessionHeaders, ...await authHeaders(endpoint) },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    throw new Error(`Negotiate HTTP error: ${res.status} ${await res.text()}`);
  }

  const body = await res.json() as { result?: { parts?: Array<{ kind: string; data?: unknown }> }; error?: unknown };
  if (body.error) throw new Error(`A2A error: ${JSON.stringify(body.error)}`);

  const parts = body.result?.parts ?? [];
  const envelope = (parts[0] as { data?: NegotiationEnvelope })?.data;
  if (!envelope) throw new Error(`Empty negotiate response: ${JSON.stringify(body)}`);

  if (!envelope.response?.accepted) {
    throw new Error(`Negotiate rejected: ${envelope.response?.reason ?? "unknown"}`);
  }
  requireNotifyContextMarker(envelope);

  return envelope;
}

function requireNotifyContextMarker(envelope: NegotiationEnvelope): string {
  const marker = envelope.response?.terms?.success_criteria;
  if (marker !== NOTIFY_CONTEXT_REQUIRED) {
    throw new Error(INVALID_NOTIFY_CONTEXT_MARKER);
  }
  return marker;
}

/** Sanitize strings for UMA claim embedding (mirrors Python _sanitize_for_claim). */
function sanitize(s: string): string {
  return s.replace(/\[/g, "(").replace(/\]/g, ")").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "");
}

/** Recursively sort object keys alphabetically (mirrors Python json.dumps sort_keys=True). */
function sortKeys(v: unknown): unknown {
  if (v === null || typeof v !== "object" || Array.isArray(v)) return v;
  const obj = v as Record<string, unknown>;
  const sorted: Record<string, unknown> = {};
  for (const k of Object.keys(obj).sort()) {
    sorted[k] = sortKeys(obj[k]);
  }
  return sorted;
}

/** Build on-chain job description from the negotiation envelope. */
export function buildJobDescription(envelope: NegotiationEnvelope): string {
  const response = envelope.response;
  const responseTerms = response.terms;
  const successCriteria = requireNotifyContextMarker(envelope);

  const terms: Record<string, unknown> = {
    deliverables: sanitize(responseTerms.deliverables ?? ""),
    quality_standards: sanitize(responseTerms.quality_standards ?? ""),
    success_criteria: sanitize(successCriteria),
  };

  const content: Record<string, unknown> = {
    version: 1,
    negotiated_at: envelope.response.negotiated_at ?? envelope.negotiated_at ?? Math.floor(Date.now() / 1000),
    task: sanitize(envelope.request.task_description),
    terms,
    price: responseTerms.price,
    currency: responseTerms.currency,
  };

  if (envelope.response.quote_expires_at != null) content["quote_expires_at"] = envelope.response.quote_expires_at;
  if (envelope.chain_id != null) content["chain_id"] = envelope.chain_id;
  if (envelope.verifying_contract) content["verifying_contract"] = envelope.verifying_contract;

  const hash = envelope.negotiation_hash ?? response.negotiation_hash ?? "";
  const sig  = envelope.provider_sig ?? response.provider_sig ?? "";
  content["negotiation_hash"] = hash;
  content["provider_sig"] = sig;

  return JSON.stringify(sortKeys(content));
}

export async function notifyFunded(
  endpoint: string,
  signer: Signer,
  jobId: bigint,
  options: NotifyOptions = {},
): Promise<string> {
  const sessionHeaders = agentCoreSessionHeaders(endpoint);
  const context = buildNotifyContext(options);
  const authorization = await createNotifyAuthorization(signer, jobId, context);
  const data = {
    skill:  "notify_funded",
    job_id: jobId.toString(),
    authorization,
  };

  const payload = {
    jsonrpc: "2.0",
    id: 2,
    method: "message/send",
    params: {
      message: {
        role: "user",
        messageId: `notify-${jobId}`,
        parts: [{ kind: "data", data }],
      },
    },
  };

  const res = await fetch(a2aUrl(endpoint), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...sessionHeaders, ...await authHeaders(endpoint) },
    body: JSON.stringify(payload),
  });

  if (!res.ok) throw new Error(`notify_funded HTTP error: ${res.status} ${await res.text()}`);
  const body = await res.json() as {
    result?: {
      parts?: Array<{ data?: { status?: string; reason?: string; retryable?: boolean; note?: string } }>;
    };
  };
  const parts = body.result?.parts ?? [];
  const ack = parts[0]?.data ?? {};
  const status = ack.status ?? "unknown";
  // A rejected/unknown ACK means the seller did NOT start delivery (bad/missing
  // authorization, unsafe gateway, verification unavailable, …). Fail loudly so
  // the caller does not fund-then-poll to a fruitless timeout.
  if (status !== "accepted") {
    const reason = ack.reason ? ` (${ack.reason})` : "";
    const retryable = ack.retryable ? " [retryable]" : "";
    throw new Error(`notify_funded not accepted: status=${status}${reason}${retryable}`);
  }
  return status;
}
