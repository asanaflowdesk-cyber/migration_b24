export default function handler(request, response) {
  if (request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return response.status(405).json({ error: "method_not_allowed" });
  }

  response.setHeader("Cache-Control", "no-store");
  return response.status(200).send(`<!doctype html>
<html lang="ru">
  <head><meta charset="utf-8"><title>Синхронизация пакетов учредителей</title></head>
  <body>
    <h1>Приложение установлено</h1>
    <p>Изменения ответственного контакта передаются в GitHub Actions.</p>
  </body>
</html>`);
}
