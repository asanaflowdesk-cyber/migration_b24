import crypto from "node:crypto";

const ALLOWED_ACTIONS = new Set(["claim", "complete", "release_dispatch"]);
const RETRYABLE_STATUSES = new Set([404, 408, 425, 429]);

function secureEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ""));
  const rightBuffer = Buffer.from(String(right || ""));
  return (
    leftBuffer.length === rightBuffer.length
    && crypto.timingSafeEqual(leftBuffer, rightBuffer)
  );
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function forwardToGoogle(queueUrl, body) {
  let lastStatus = 502;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const upstream = await fetch(queueUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        redirect: "follow",
        signal: AbortSignal.timeout(4000),
      });
      lastStatus = upstream.status;
      const text = await upstream.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = {};
      }
      const retryable = RETRYABLE_STATUSES.has(upstream.status) || upstream.status >= 500;
      if (upstream.ok && data && data.ok) return { ok: true, data };
      if (!retryable || attempt === 2) {
        console.error("Google queue rejected request", body.action, upstream.status);
        return { ok: false, status: upstream.status || 502 };
      }
    } catch (error) {
      console.error(
        "Google queue request failed",
        body.action,
        error instanceof Error ? error.name : "unknown",
      );
      if (attempt === 2) return { ok: false, status: 502 };
    }
    await sleep(250);
  }
  return { ok: false, status: lastStatus };
}

export default async function handler(request, response) {
  response.setHeader("Cache-Control", "no-store");
  if (request.method !== "POST") {
    response.setHeader("Allow", "POST");
    return response.status(405).json({ error: "method_not_allowed" });
  }

  const body = request.body && typeof request.body === "object" ? request.body : {};
  const queueKey = String(process.env.GOOGLE_QUEUE_KEY || "").trim();
  const queueUrl = String(process.env.GOOGLE_QUEUE_URL || "").trim();

  if (
    queueKey.length < 32
    || !secureEqual(body.key, queueKey)
  ) {
    return response.status(401).json({ error: "unauthorized" });
  }
  if (!queueUrl.startsWith("https://script.google.com/macros/s/")) {
    return response.status(500).json({ error: "google_queue_not_configured" });
  }
  if (!ALLOWED_ACTIONS.has(String(body.action || ""))) {
    return response.status(400).json({ error: "invalid_action" });
  }

  const result = await forwardToGoogle(queueUrl, body);
  if (!result.ok) {
    return response.status(502).json({
      error: "google_queue_unavailable",
      upstream_status: result.status,
    });
  }
  return response.status(200).json(result.data);
}
