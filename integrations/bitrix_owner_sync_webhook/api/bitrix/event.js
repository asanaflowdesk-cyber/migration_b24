import crypto from "node:crypto";

function secureEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ""));
  const rightBuffer = Buffer.from(String(right || ""));
  return (
    leftBuffer.length === rightBuffer.length
    && crypto.timingSafeEqual(leftBuffer, rightBuffer)
  );
}

function normalizeDomain(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/\/$/, "");
}

function valueAt(body, nestedPath, flatKey) {
  let current = body;
  for (const key of nestedPath) {
    current = current && typeof current === "object" ? current[key] : undefined;
  }
  return current ?? body?.[flatKey];
}

function numericId(value) {
  const raw = String(value || "").trim();
  return /^\d+$/.test(raw) && Number(raw) > 0 ? raw : "";
}

function parseBody(request) {
  if (request.body && typeof request.body === "object") {
    return request.body;
  }
  if (typeof request.body === "string") {
    return Object.fromEntries(new URLSearchParams(request.body));
  }
  return {};
}

export default async function handler(request, response) {
  if (request.method !== "POST") {
    response.setHeader("Allow", "POST");
    return response.status(405).json({ error: "method_not_allowed" });
  }

  const expectedKey = String(process.env.BITRIX_WEBHOOK_KEY || "").trim();
  const requestUrl = new URL(request.url, "https://webhook.invalid");
  if (!expectedKey || !secureEqual(requestUrl.searchParams.get("key"), expectedKey)) {
    return response.status(401).json({ error: "invalid_webhook_key" });
  }

  const body = parseBody(request);
  const event = String(body.event || "").trim().toUpperCase();
  const contactId = numericId(
    valueAt(body, ["data", "FIELDS", "ID"], "data[FIELDS][ID]"),
  );
  const actualDomain = normalizeDomain(
    valueAt(body, ["auth", "domain"], "auth[domain]"),
  );
  const expectedDomain = normalizeDomain(process.env.BITRIX_ALLOWED_DOMAIN);

  if (event !== "ONCRMCONTACTUPDATE") {
    return response.status(202).json({ accepted: false, reason: "event_ignored" });
  }
  if (!contactId) {
    return response.status(400).json({ error: "missing_contact_id" });
  }
  if (!expectedDomain || actualDomain !== expectedDomain) {
    return response.status(403).json({ error: "unexpected_bitrix_domain" });
  }

  const githubToken = String(process.env.GITHUB_DISPATCH_TOKEN || "").trim();
  const githubRepository = String(process.env.GITHUB_REPOSITORY || "").trim();
  if (!githubToken || !/^[^/\s]+\/[^/\s]+$/.test(githubRepository)) {
    return response.status(500).json({ error: "github_dispatch_not_configured" });
  }

  const githubResponse = await fetch(
    `https://api.github.com/repos/${githubRepository}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${githubToken}`,
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bitrix-owner-sync-webhook",
      },
      body: JSON.stringify({
        event_type: "founder_owner_changed",
        client_payload: {
          contact_id: contactId,
          bitrix_domain: actualDomain,
          event_ts: String(body.ts || ""),
        },
      }),
    },
  );

  if (!githubResponse.ok) {
    const githubError = (await githubResponse.text()).slice(0, 500);
    console.error("GitHub dispatch failed", githubResponse.status, githubError);
    return response.status(502).json({
      error: "github_dispatch_failed",
      status: githubResponse.status,
    });
  }

  return response.status(202).json({ accepted: true, contact_id: contactId });
}
