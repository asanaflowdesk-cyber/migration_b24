<?php
declare(strict_types=1);
ini_set('display_errors', '0');

$configPath = getenv('FLOWDESK_ADMIN_PORTAL_CONFIG') ?: '/etc/deskflow-admin.php';
$bridgePath = dirname($configPath) . '/deskflow-bridge.php';
if (!is_readable($bridgePath)) {
    http_response_code(404);
    exit;
}
require $bridgePath;
define('STOP_STATISTICS', true);
ob_start();
require deskflow_prolog_path();
ob_end_clean();
deskflow_response_headers();
[$uid, $sessionId] = deskflow_identity();

$read = [
    '/' => 'text/html; charset=utf-8',
    '/assets/app.js' => 'text/javascript; charset=utf-8',
    '/assets/graph.js' => 'text/javascript; charset=utf-8',
    '/api/versions' => 'application/json; charset=utf-8',
    '/assets/app.css' => 'text/css; charset=utf-8',
    '/api/draft' => 'application/json; charset=utf-8',
    '/api/access' => 'application/json; charset=utf-8',
];
$write = [
    '/api/draft', '/api/validate', '/api/graph', '/api/preview',
    '/api/access/grant', '/api/access/revoke',
    '/api/publish', '/api/rollback', '/api/journal', '/api/journal/request',
];
$route = $_GET['r'] ?? '/';
$method = (string) ($_SERVER['REQUEST_METHOD'] ?? '');
if (!is_string($route)) {
    deskflow_not_found();
}
if ($method === 'GET' && array_key_exists($route, $read)) {
    $mime = $read[$route];
} elseif ($method === 'POST' && in_array($route, $write, true)) {
    $mime = 'application/json; charset=utf-8';
} else {
    deskflow_not_found();
}

$body = '';
if ($method === 'POST') {
    $csrf = (string) ($_SERVER['HTTP_X_DESKFLOW_CSRF'] ?? '');
    $mediaType = strtolower(trim(explode(';', (string) ($_SERVER['CONTENT_TYPE'] ?? ''), 2)[0]));
    if ($sessionId === '' || !hash_equals($sessionId, $csrf) || $mediaType !== 'application/json') {
        deskflow_not_found();
    }
    $body = file_get_contents('php://input', false, null, 0, DESKFLOW_BODY_LIMIT + 1);
    if ($body === false || strlen($body) > DESKFLOW_BODY_LIMIT) {
        deskflow_not_found();
    }
}
deskflow_close_session();
[$status, $response] = deskflow_bridge($method, $route, $body, $uid, $configPath);
if ($route === '/' && $status === 200) {
    $response = str_replace('__DESKFLOW_CSRF__', htmlspecialchars($sessionId, ENT_QUOTES, 'UTF-8'), $response);
}
http_response_code($status);
header('Content-Type: ' . $mime);
echo $response;
