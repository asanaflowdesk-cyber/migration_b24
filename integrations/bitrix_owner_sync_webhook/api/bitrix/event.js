import crypto from "node:crypto";

const GITHUB_DISPATCH_ATTEMPTS = 3;
const GITHUB_DISPATCH_TIMEOUT_MS = 3000;
const GITHUB_RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);
const GITHUB_RETRY_DELAYS_MS = [250, 750];

function secureEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ""));
  const rightBuffer = Buffer.from(String(right || ""));
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function normalizeDomain(value) {
  return String(value || "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function valueAt(body, nestedPath, flatKey) {
  let current = body;
  for (const key of nestedPath) current = current && typeof current === "object" ? current[key] : undefined;
  return current ?? body?.[flatKey];
}

function numericId(value) {
  const raw = String(value || "").trim();
  return /^\d+$/.test(raw) && Number(raw) > 0 ? raw : "";
}

function parseBody(request) {
  if (request.body && typeof request.body === "object") return request.body;
  if (typeof request.body === "string") return Object.fromEntries(new URLSearchParams(request.body));
  return {};
}

function isFounderContact(contact) {
  const post = String(contact?.POST || "").toLocaleLowerCase("ru-RU");
  const comments = String(contact?.COMMENTS || "");
  return post.includes("руковод") || post.includes("учред") || comments.includes("EQAZYNA_DIRECTOR:");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchChangedContact(body, contactId) {
  const endpoint = String(valueAt(body, ["auth", "client_endpoint"], "auth[client_endpoint]") || "").trim();
  const token = String(valueAt(body, ["auth", "access_token"], "auth[access_token]") || "").trim();
  if (!endpoint.startsWith("https://") || !token) return null;
  try {
    const response = await fetch(`${endpoint.replace(/\/$/, "")}/crm.contact.get.json`, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: new URLSearchParams({id: contactId, auth: token}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data || typeof data.result !== "object") return null;
    return data.result;
  } catch {
    return null;
  }
}

async function queueRequest(action, payload = {}) {
  const queueUrl = String(process.env.GOOGLE_QUEUE_URL || "").trim();
  const queueKey = String(process.env.GOOGLE_QUEUE_KEY || "").trim();
  if (!queueUrl.startsWith("https://script.google.com/macros/s/") || queueKey.length < 32) throw new Error("google_queue_not_configured");
  const result = await fetch(queueUrl, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({key: queueKey, action, ...payload}),
  });
  const data = await result.json().catch(() => ({}));
  if (!result.ok || !data.ok) throw new Error(`google_queue_${action}_failed`);
  return data;
}

async function recordDispatchError({contactId, version, error, status = 0, attempts = 0}) {
  const payload = {
    contact_id: contactId,
    version: Number(version || 0),
    error: String(error || "github_dispatch_failed").slice(0, 120),
    status: Number(status || 0),
    attempts: Number(attempts || 0),
  };
  try {
    await queueRequest("dispatch_error", payload);
    return;
  } catch (queueError) {
    console.error("Queue dispatch_error failed", queueError instanceof Error ? queueError.message : "unknown");
  }
  await queueRequest("release_dispatch").catch(() => {});
}

async function dispatchGithub({repository, token, actualDomain, queueVersion}) {
  let lastStatus = 0;
  let lastError = "github_dispatch_failed";

  for (let attempt = 1; attempt <= GITHUB_DISPATCH_ATTEMPTS; attempt += 1) {
    try {
      const githubResponse = await fetch(`https://api.github.com/repos/${repository}/dispatches`, {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "bitrix-owner-sync-webhook",
        },
        body: JSON.stringify({
          event_type: "founder_batch_ready",
          client_payload: {bitrix_domain: actualDomain, queue_version: String(queueVersion || "")},
        }),
        signal: AbortSignal.timeout(GITHUB_DISPATCH_TIMEOUT_MS),
      });

      lastStatus = githubResponse.status;
      if (githubResponse.ok) {
        return {ok: true, status: githubResponse.status, attempts: attempt, error: ""};
      }

      const body = (await githubResponse.text()).replace(/\s+/g, " ").trim().slice(0, 300);
      lastError = body ? `github_http_${githubResponse.status}:${body}` : `github_http_${githubResponse.status}`;
      console.error("GitHub dispatch failed", githubResponse.status, `attempt=${attempt}/${GITHUB_DISPATCH_ATTEMPTS}`, body);

      if (!GITHUB_RETRYABLE_STATUSES.has(githubResponse.status)) {
        return {ok: false, status: githubResponse.status, attempts: attempt, error: lastError};
      }
    } catch (error) {
      lastStatus = 0;
      lastError = error instanceof Error ? `github_network_${error.name}` : "github_network_error";
      console.error("GitHub dispatch request failed", `attempt=${attempt}/${GITHUB_DISPATCH_ATTEMPTS}`, lastError);
    }

    if (attempt < GITHUB_DISPATCH_ATTEMPTS) {
      await sleep(GITHUB_RETRY_DELAYS_MS[Math.min(attempt - 1, GITHUB_RETRY_DELAYS_MS.length - 1)]);
    }
  }

  return {ok: false, status: lastStatus, attempts: GITHUB_DISPATCH_ATTEMPTS, error: lastError};
}

export default async function handler(request, response) {
  if (request.method !== "POST") {
    response.setHeader("Allow", "POST");
    return response.status(405).json({error: "method_not_allowed"});
  }

  const expectedKey = String(process.env.BITRIX_WEBHOOK_KEY || "").trim();
  const requestUrl = new URL(request.url, "https://webhook.invalid");
  if (!expectedKey || !secureEqual(requestUrl.searchParams.get("key"), expectedKey)) return response.status(401).json({error: "invalid_webhook_key"});

  const body = parseBody(request);
  const event = String(body.event || "").trim().toUpperCase();
  const contactId = numericId(valueAt(body, ["data", "FIELDS", "ID"], "data[FIELDS][ID]"));
  const actualDomain = normalizeDomain(valueAt(body, ["auth", "domain"], "auth[domain]"));
  const expectedDomain = normalizeDomain(process.env.BITRIX_ALLOWED_DOMAIN);

  if (event !== "ONCRMCONTACTUPDATE") return response.status(202).json({accepted: false, reason: "event_ignored"});
  if (!contactId) return response.status(400).json({error: "missing_contact_id"});
  if (!expectedDomain || actualDomain !== expectedDomain) return response.status(403).json({error: "unexpected_bitrix_domain"});

  const changedContact = await fetchChangedContact(body, contactId);
  if (changedContact && !isFounderContact(changedContact)) {
    return response.status(202).json({accepted: true, queued: false, dispatched: false, reason: "ordinary_contact", contact_id: contactId});
  }

  let queued;
  try {
    queued = await queueRequest("enqueue", {
      contact_id: contactId,
      event_ts: String(body.ts || ""),
      owner_id: numericId(changedContact?.ASSIGNED_BY_ID),
    });
  } catch (error) {
    console.error("Queue enqueue failed", error instanceof Error ? error.message : "unknown");
    return response.status(502).json({error: "queue_enqueue_failed"});
  }

  if (!queued.dispatch) {
    return response.status(202).json({
      accepted: true,
      queued: queued.queued !== false,
      dispatched: false,
      reason: String(queued.reason || "queued_without_dispatch"),
      contact_id: contactId,
    });
  }

  const githubToken = String(process.env.GITHUB_DISPATCH_TOKEN || "").trim();
  const githubRepository = String(process.env.GITHUB_REPOSITORY || "").trim();
  if (!githubToken || !/^[^/\s]+\/[^/\s]+$/.test(githubRepository)) {
    await recordDispatchError({
      contactId,
      version: queued.version,
      error: "github_dispatch_not_configured",
      attempts: 0,
    });
    return response.status(500).json({error: "github_dispatch_not_configured"});
  }

  const dispatched = await dispatchGithub({
    repository: githubRepository,
    token: githubToken,
    actualDomain,
    queueVersion: queued.version,
  });

  if (!dispatched.ok) {
    await recordDispatchError({
      contactId,
      version: queued.version,
      error: dispatched.error,
      status: dispatched.status,
      attempts: dispatched.attempts,
    });
    return response.status(502).json({
      error: "github_dispatch_failed",
      status: dispatched.status,
      attempts: dispatched.attempts,
    });
  }

  return response.status(202).json({
    accepted: true,
    queued: true,
    dispatched: true,
    contact_id: contactId,
    dispatch_attempts: dispatched.attempts,
  });
}
