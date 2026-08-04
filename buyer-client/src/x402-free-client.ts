export interface FreeQuoteResponse {
  content: string;
  format: "markdown" | "text";
}


export async function readFreeQuoteResponse(response: Response): Promise<string> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new Error("Invalid free quote response");
  }
  if (typeof value !== "object" || value === null) {
    throw new Error("Invalid free quote response");
  }
  const record = value as Record<string, unknown>;
  if (
    typeof record["content"] !== "string"
    || record["content"] === ""
    || !["markdown", "text"].includes(String(record["format"]))
  ) {
    throw new Error("Invalid free quote response");
  }
  return (record as unknown as FreeQuoteResponse).content;
}
