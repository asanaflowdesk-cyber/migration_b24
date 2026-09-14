function normalizedBaseUrl(request) {
  const configured = String(process.env.PUBLIC_BASE_URL || "").trim().replace(/\/$/, "");
  const host = String(request.headers["x-forwarded-host"] || request.headers.host || "").trim();
  const value = configured || (host ? `https://${host}` : "");
  if (!value.startsWith("https://")) {
    throw new Error("PUBLIC_BASE_URL must be an HTTPS URL");
  }
  return value;
}

function safeJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

export default function handler(request, response) {
  if (request.method !== "GET" && request.method !== "POST") {
    response.setHeader("Allow", "GET, POST");
    return response.status(405).json({ error: "method_not_allowed" });
  }

  const webhookKey = String(process.env.BITRIX_WEBHOOK_KEY || "").trim();
  if (!webhookKey) {
    return response.status(500).send("BITRIX_WEBHOOK_KEY is not configured");
  }

  let eventHandlerUrl;
  try {
    const baseUrl = normalizedBaseUrl(request);
    eventHandlerUrl = `${baseUrl}/api/bitrix/event?key=${encodeURIComponent(webhookKey)}`;
  } catch (error) {
    return response.status(500).send(String(error.message || error));
  }

  response.setHeader("Cache-Control", "no-store");
  response.setHeader(
    "Content-Security-Policy",
    "default-src 'none'; script-src https://api.bitrix24.com 'unsafe-inline'; style-src 'unsafe-inline'; connect-src *",
  );
  return response.status(200).send(`<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <title>Установка синхронизации пакетов учредителей</title>
    <script src="https://api.bitrix24.com/api/v1/"></script>
    <style>body{font:16px Arial,sans-serif;padding:24px;color:#222}#status{white-space:pre-wrap}</style>
  </head>
  <body>
    <h1>Установка обработчика</h1>
    <p id="status">Подключение к Bitrix24…</p>
    <script>
      const eventHandlerUrl = ${safeJson(eventHandlerUrl)};
      const status = document.getElementById("status");

      function finishWithError(message) {
        status.textContent = "Ошибка установки: " + message;
      }

      BX24.init(function () {
        BX24.callMethod(
          "event.unbind",
          { event: "ONCRMCONTACTUPDATE", handler: eventHandlerUrl },
          function () {
            BX24.callMethod(
              "event.bind",
              { event: "ONCRMCONTACTUPDATE", handler: eventHandlerUrl },
              function (result) {
                if (result.error()) {
                  finishWithError(result.error() + ": " + result.error_description());
                  return;
                }
                status.textContent = "Обработчик ONCRMCONTACTUPDATE зарегистрирован.";
                BX24.installFinish();
              }
            );
          }
        );
      });
    </script>
  </body>
</html>`);
}
