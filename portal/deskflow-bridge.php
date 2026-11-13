<?php
declare(strict_types=1);
ini_set('display_errors', '0');

const DESKFLOW_BODY_LIMIT = 2000000;

function deskflow_not_found(): void
{
    http_response_code(404);
    header('Cache-Control: no-store');
    header('Content-Type: application/json; charset=utf-8');
    echo '{"error":"Страница не найдена"}';
    exit;
}

function deskflow_response_headers(): void
{
    header('Cache-Control: private, no-store, max-age=0');
    header('Referrer-Policy: no-referrer');
    header('X-Robots-Tag: noindex, nofollow, noarchive');
    header('X-Content-Type-Options: nosniff');
    header('X-Frame-Options: SAMEORIGIN');
    header("Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'");
}

function deskflow_prolog_path(): string
{
    $portalRoot = rtrim((string) ($_SERVER['DOCUMENT_ROOT'] ?? ''), "/\\");
    if ($portalRoot === '') {
        deskflow_not_found();
    }
    $prolog = $portalRoot . '/bitrix/modules/main/include/prolog_before.php';
    if (!is_file($prolog)) {
        deskflow_not_found();
    }
    return $prolog;
}

function deskflow_identity(): array
{
    global $USER;
    if (!isset($USER) || !$USER->IsAuthorized()) {
        deskflow_not_found();
    }
    $uid = (string) $USER->GetID();
    if (!preg_match('/\A[1-9][0-9]{0,9}\z/D', $uid)) {
        deskflow_not_found();
    }
    return [$uid, (string) bitrix_sessid()];
}

function deskflow_close_session(): void
{
    if (session_status() === PHP_SESSION_ACTIVE) {
        session_write_close();
    }
}

function deskflow_bridge(string $method, string $route, string $body, string $uid, string $configPath): array
{
    if (!is_readable($configPath) || !extension_loaded('curl')) {
        deskflow_not_found();
    }
    $config = require $configPath;
    if (!is_array($config)) {
        deskflow_not_found();
    }
    $upstream = rtrim((string) ($config['upstream'] ?? ''), '/');
    $keyHex = (string) ($config['key_hex'] ?? '');
    $uri = parse_url($upstream);
    $scheme = $uri['scheme'] ?? '';
    if (
        !is_array($uri) || empty($uri['host'])
        || isset($uri['user']) || isset($uri['pass'])
        || isset($uri['query']) || isset($uri['fragment']) || !empty($uri['path'])
        || ($scheme !== 'https' && !($scheme === 'http' && $uri['host'] === '127.0.0.1'))
        || !preg_match('/\A[0-9a-fA-F]{64}\z/D', $keyHex)
    ) {
        deskflow_not_found();
    }

    $stamp = (string) time();
    $nonce = bin2hex(random_bytes(16));
    $signed = implode("\n", [
        'deskflow-admin-v1', $uid, $stamp, $nonce, $method, $route, hash('sha256', $body),
    ]);
    $key = hex2bin($keyHex);
    $signature = hash_hmac('sha256', $signed, $key);
    $headers = [
        'X-DeskFlow-User: ' . $uid,
        'X-DeskFlow-Time: ' . $stamp,
        'X-DeskFlow-Nonce: ' . $nonce,
        'X-DeskFlow-Signature: ' . $signature,
    ];
    $curl = curl_init($upstream . $route);
    if ($curl === false) {
        deskflow_not_found();
    }
    $options = [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
        CURLOPT_PROXY => '',
        CURLOPT_CONNECTTIMEOUT => 3,
        CURLOPT_TIMEOUT => 12,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
    ];
    $responseSignatures = [];
    $options[CURLOPT_HEADERFUNCTION] = static function ($handle, string $line) use (&$responseSignatures): int {
        $prefix = 'X-DeskFlow-Response-Signature:';
        if (stripos($line, $prefix) === 0) {
            $responseSignatures[] = trim(substr($line, strlen($prefix)));
        }
        return strlen($line);
    };
    if ($method === 'POST') {
        $headers[] = 'Content-Type: application/json';
        $options[CURLOPT_POST] = true;
        $options[CURLOPT_POSTFIELDS] = $body;
    }
    if (!empty($config['ca_file'])) {
        $options[CURLOPT_CAINFO] = (string) $config['ca_file'];
    }
    $options[CURLOPT_HTTPHEADER] = $headers;
    curl_setopt_array($curl, $options);
    $response = curl_exec($curl);
    $status = (int) curl_getinfo($curl, CURLINFO_HTTP_CODE);
    curl_close($curl);
    if (!is_string($response) || !in_array($status, [200, 400, 409], true)) {
        deskflow_not_found();
    }
    $signedReply = implode("\n", ['deskflow-admin-response-v1', $nonce, (string) $status, hash('sha256', $response)]);
    if (count($responseSignatures) !== 1 || !hash_equals(hash_hmac('sha256', $signedReply, $key), $responseSignatures[0])) {
        deskflow_not_found();
    }
    return [$status, $response];
}
