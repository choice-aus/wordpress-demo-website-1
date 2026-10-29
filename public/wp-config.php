<?php

// Load local overrides FIRST
if (file_exists(__DIR__ . '/wp-config-local.php')) {
    require __DIR__ . '/wp-config-local.php';
}

//define('WP_HOME', 'https://mysite1.test:4441');
//define('WP_SITEURL', WP_HOME . '/wp');

// FORCE URLs (override everything)
if (!defined('WP_HOME')) {
    define('WP_HOME', 'https://mysite1.test:4441');
}

if (!defined('WP_SITEURL')) {
    define('WP_SITEURL', 'https://mysite1.test:4441/wp');
}

// Custom content directory
define('WP_CONTENT_DIR', __DIR__ . '/wp-content');
define('WP_CONTENT_URL', WP_HOME . '/wp-content');

//define('WP_CONTENT_URL', '/wp-content');

define('DB_NAME', getenv('DB_NAME') ?: 'wordpress');
define('DB_USER', getenv('DB_USER') ?: 'wordpress');
define('DB_PASSWORD', getenv('DB_PASSWORD') ?: 'wordpress');
define('DB_HOST', 'db');

// Other global settings
defined('WP_DEBUG') || define('WP_DEBUG', true);

define('WP_MAIL_SMTP_HOST', 'mailhog');
define('WP_MAIL_SMTP_PORT', getenv('MAILHOG_SMTP_PORT') ?: 1025);

// Caching
define('WP_CACHE', true);

define('WP_OBJECT_CACHE_HOST', 'memcached');
define('WP_OBJECT_CACHE_PORT', 11211);
define('WP_OBJECT_CACHE_GROUP', 'mysite1');

// this is the right one for Object Cache 4 everyone
define('WP_MEMCACHED_SERVERS', [
    'default' => ['memcached:11211']
]);

define('WP_CACHE_KEY_SALT', 'mysite1:');

// THIS is the critical one:
define('WP_USE_EXT_OBJECT_CACHE', true);

define('WP_REDIS_DISABLED', true);

define('OC4EVERYONE_MEMCACHED_SERVER', 'memcached:11211');

// define('WP_MEMCACHED_USE_MEMCACHED', true);
// define('WP_MEMCACHE_USE_MEMCACHED', true);

$table_prefix = 'wp_';

define('FS_METHOD', 'direct');

/* That's all, stop editing! */

if (!defined('ABSPATH')) {
    define('ABSPATH', __DIR__ . '/wp/');
}

require_once ABSPATH . 'wp-settings.php';