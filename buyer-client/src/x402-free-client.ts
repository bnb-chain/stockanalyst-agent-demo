export interface FreeQuoteResponse {
  symbol: string;
  name?: string | null;
  date?: string | null;
  currency?: string | null;
  price?: number | null;
  price_now?: number | null;
  change_pct?: number | null;
  market_cap?: number | null;
  pe_ratio?: number | null;
  forward_pe?: number | null;
  analyst_target?: number | null;
  twelve_month_target?: number | null;
  analyst_upside_pct?: number | null;
  room_to_target?: number | null;
  recommendation?: string | null;
  beta?: number | null;
  week_52_low?: number | null;
  week_52_high?: number | null;
  error?: string;
}


export async function readFreeQuoteResponse(
  response: Response,
): Promise<FreeQuoteResponse> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new Error("Invalid free quote response");
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Invalid free quote response");
  }
  const record = value as Record<string, unknown>;
  if (typeof record["symbol"] !== "string" || record["symbol"] === "") {
    throw new Error("Invalid free quote response");
  }
  return record as unknown as FreeQuoteResponse;
}
