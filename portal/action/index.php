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
[$uid] = deskflow_identity();
$token = $_GET['t'] ?? '';
$purpose = strtolower((string) ($_SERVER['HTTP_SEC_PURPOSE'] ?? ''));
if (
    ($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET' || strpos($purpose, 'prefetch') !== false
    || !is_string($token) || !preg_match('/\A[A-Za-z0-9_-]{1,500}\.[0-9a-f]{64}\z/D', $token)
) {
    deskflow_not_found();
}
deskflow_close_session();
[$status, $response] = deskflow_bridge('POST', '/api/resource', json_encode(['token' => $token]), $uid, $configPath);
$payload = json_decode($response, true);
$location = $payload['location'] ?? '';
if (
    $status !== 200 || !is_string($location) || strlen($location) > 2048
    || preg_match('/[\x00-\x20\x7f]/', $location) || strpos($location, chr(92)) !== false
    || !preg_match('~\A(?:https://|/(?!/))~', $location)
) {
    deskflow_not_found();
}
header('Location: ' . $location, true, 303);
exit;
