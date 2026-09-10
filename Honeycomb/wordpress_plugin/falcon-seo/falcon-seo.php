<?php
/**
 * Plugin Name: TechShu SEO Bridge
 * Plugin URI:  https://bringdata.a.techshu.in
 * Description: Connects this WordPress site to the Falcon MCP portal for AI-assisted management — content, SEO (Yoast-compatible), media, image optimization, menus, themes (incl. FSE), users, settings, security hardening, performance, backups & WooCommerce. Changes are applied live and logged here.
 * Version:     2.1.0
 * Requires at least: 5.6
 * Requires PHP: 7.4
 * Author:      TechShu
 * Author URI:  https://techshu.in
 * License:     GPLv2 or later
 * License URI: https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain: techshu-seo-bridge
 */

if (!defined('ABSPATH')) {
    exit; // No direct access.
}

define('FALCON_SEO_VERSION', '2.1.0');
define('FALCON_SEO_TABLE', 'falcon_pending');

/* ============================================================
 * Activation — create the pending-changes table + a token
 * ============================================================ */
register_activation_hook(__FILE__, 'falcon_seo_activate');
function falcon_seo_activate() {
    global $wpdb;
    $table = $wpdb->prefix . FALCON_SEO_TABLE;
    $charset = $wpdb->get_charset_collate();
    $sql = "CREATE TABLE $table (
        id BIGINT(20) UNSIGNED NOT NULL AUTO_INCREMENT,
        post_id BIGINT(20) UNSIGNED NOT NULL,
        change_type VARCHAR(32) NOT NULL,
        payload LONGTEXT NOT NULL,
        reason TEXT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY  (id),
        KEY status (status)
    ) $charset;";
    require_once ABSPATH . 'wp-admin/includes/upgrade.php';
    dbDelta($sql);

    if (!get_option('falcon_seo_token')) {
        update_option('falcon_seo_token', wp_generate_password(40, false, false));
    }

    // Onboarding fix: make sure the Authorization header reaches PHP (many hosts strip it).
    falcon_seo_write_htaccess();
}

// v2.0.0 removed the code-snippet and mu-plugin-based SMTP features (they wrote
// caller-supplied PHP to disk). Sites upgrading from an earlier version may still have
// those generated files sitting in mu-plugins, still auto-loading — clean them up once.
add_action('admin_init', 'falcon_seo_cleanup_legacy_files');
function falcon_seo_cleanup_legacy_files() {
    if (get_option('falcon_seo_legacy_cleanup_done')) return;
    foreach (array('falcon-smtp.php', 'falcon-snippets-loader.php', 'falcon-hardening.php') as $f) {
        $path = WPMU_PLUGIN_DIR . '/' . $f;
        if (file_exists($path)) @unlink($path);
    }
    $snippets_dir = WPMU_PLUGIN_DIR . '/falcon-snippets';
    if (is_dir($snippets_dir)) {
        foreach (glob($snippets_dir . '/*.php') as $f) @unlink($f);
        @rmdir($snippets_dir);
    }
    delete_option('falcon_seo_snippets');
    update_option('falcon_seo_legacy_cleanup_done', 1);
}

function falcon_seo_table() {
    global $wpdb;
    return $wpdb->prefix . FALCON_SEO_TABLE;
}

/* ============================================================
 * Onboarding: auto-fix the Authorization-header-stripping problem
 * by adding a rewrite rule to the site's .htaccess on activation.
 * ============================================================ */
function falcon_seo_write_htaccess() {
    if (!function_exists('insert_with_markers')) {
        require_once ABSPATH . 'wp-admin/includes/misc.php';
    }
    $htaccess = ABSPATH . '.htaccess';
    if (file_exists($htaccess)) {
        if (!is_writable($htaccess)) return false;
    } elseif (!is_writable(ABSPATH)) {
        return false;
    }
    $lines = array(
        '<IfModule mod_rewrite.c>',
        'RewriteEngine On',
        'RewriteCond %{HTTP:Authorization} ^(.*)',
        'RewriteRule ^(.*) - [E=HTTP_AUTHORIZATION:%1]',
        '</IfModule>',
        'SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1',
    );
    return insert_with_markers($htaccess, 'Falcon SEO', $lines);
}

/* Read the Authorization header from every place a host might leave it. */
function falcon_seo_get_auth_header() {
    if (isset($_SERVER['HTTP_AUTHORIZATION'])) return $_SERVER['HTTP_AUTHORIZATION'];
    if (isset($_SERVER['REDIRECT_HTTP_AUTHORIZATION'])) return $_SERVER['REDIRECT_HTTP_AUTHORIZATION'];
    if (function_exists('getallheaders')) {
        foreach (getallheaders() as $k => $v) {
            if (strtolower($k) === 'authorization') return $v;
        }
    }
    return '';
}

/* ============================================================
 * Auth — Bearer token check for every REST call
 * ============================================================ */
function falcon_seo_auth(WP_REST_Request $request) {
    $stored = get_option('falcon_seo_token');
    if (!$stored) {
        return new WP_Error('falcon_no_token', 'Plugin not configured.', array('status' => 403));
    }
    $header = $request->get_header('authorization');
    if (!$header) {
        $header = falcon_seo_get_auth_header();
    }
    $token = '';
    if ($header && preg_match('/Bearer\s+(.+)/i', $header, $m)) {
        $token = trim($m[1]);
    }
    if (!$token || !hash_equals($stored, $token)) {
        return new WP_Error('falcon_bad_token', 'Invalid token.', array('status' => 401));
    }
    return true;
}

/* ============================================================
 * REST access override — survive "disable REST API" hardening
 * ------------------------------------------------------------
 * Some security plugins / snippets block ALL /wp-json/ requests from
 * non-logged-in users through the `rest_authentication_errors` filter
 * (WordPress then returns `rest_not_logged_in` — "You are not currently
 * logged in."). That filter runs BEFORE our route permission_callback,
 * so it rejects the Falcon request before the Bearer token is ever
 * checked — the connector sees a 401 it can't get past.
 *
 * We register on the same filter at the lowest priority (runs last) and
 * clear that error ONLY for requests that carry a valid Falcon Bearer
 * token. Anonymous / wrong-token requests keep whatever result the
 * security layer set, so the hardening stays fully intact for everyone
 * else. No site settings are touched.
 * ============================================================ */
add_filter('rest_authentication_errors', 'falcon_seo_rest_access_override', PHP_INT_MAX);
function falcon_seo_rest_access_override($result) {
    // Already authenticated by something else — don't interfere.
    if ($result === true) {
        return $result;
    }
    $stored = get_option('falcon_seo_token');
    if (!$stored) {
        return $result; // plugin not configured yet — stay out of the way
    }
    $header = falcon_seo_get_auth_header();
    if (!$header || !preg_match('/Bearer\s+(.+)/i', $header, $m)) {
        return $result; // no Falcon token present — blocked stays blocked
    }
    if (hash_equals($stored, trim($m[1]))) {
        // Valid Falcon token: clear any lockdown error for THIS request only.
        // The route's permission_callback still re-validates the token.
        return true;
    }
    return $result;
}

/* ============================================================
 * Yoast helpers
 * ============================================================ */
function falcon_seo_yoast_active() {
    return defined('WPSEO_VERSION') || is_plugin_active('wordpress-seo/wp-seo.php');
}

function falcon_seo_get_meta($post_id) {
    return array(
        'seo_title'        => get_post_meta($post_id, '_yoast_wpseo_title', true),
        'meta_description' => get_post_meta($post_id, '_yoast_wpseo_metadesc', true),
        'focus_keyword'    => get_post_meta($post_id, '_yoast_wpseo_focuskw', true),
    );
}

function falcon_seo_post_dto($post, $with_content = false) {
    $meta = falcon_seo_get_meta($post->ID);
    $dto = array(
        'id'        => $post->ID,
        'type'      => $post->post_type,
        'status'    => $post->post_status,
        'title'     => get_the_title($post),
        'url'       => get_permalink($post),
        'modified'  => $post->post_modified_gmt,
        'yoast'     => $meta,
    );
    if ($with_content) {
        $dto['content'] = $post->post_content;
        $dto['excerpt'] = $post->post_excerpt;
        $dto['word_count'] = str_word_count(wp_strip_all_tags($post->post_content));
    }
    return $dto;
}

/* ============================================================
 * Front-end runtime hooks (managed from Falcon)
 * ============================================================ */

/* 301/302 redirects stored by the redirect manager. */
add_action('template_redirect', 'falcon_seo_do_redirects', 1);
function falcon_seo_do_redirects() {
    if (is_admin()) return;
    $redirects = get_option('falcon_seo_redirects', array());
    if (!$redirects) return;
    $req = untrailingslashit(parse_url($_SERVER['REQUEST_URI'] ?? '', PHP_URL_PATH));
    foreach ($redirects as $r) {
        $from_path = parse_url($r['from'], PHP_URL_PATH);
        $from = untrailingslashit($from_path ? $from_path : $r['from']);
        if ($from !== '' && $req === $from) {
            wp_redirect($r['to'], (int) ($r['type'] ?? 301));
            exit;
        }
    }
}

/* Custom JSON-LD schema per post. */
add_action('wp_head', 'falcon_seo_print_schema');
function falcon_seo_print_schema() {
    if (!is_singular()) return;
    $schema = get_post_meta(get_queried_object_id(), '_falcon_schema_jsonld', true);
    if (!$schema) return;
    $decoded = json_decode($schema, true);
    if (json_last_error() !== JSON_ERROR_NONE) return;
    echo "\n<script type=\"application/ld+json\">" . wp_json_encode($decoded, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) . "</script>\n";
}

/* OpenGraph / Twitter card tags — only when Yoast isn't already handling them. */
add_action('wp_head', 'falcon_seo_print_social', 5);
function falcon_seo_print_social() {
    if (!is_singular() || falcon_seo_yoast_active()) return;
    $s = get_post_meta(get_queried_object_id(), '_falcon_social', true);
    if (!$s || !is_array($s)) return;
    $tags = array(
        'og:title' => $s['og_title'] ?? '', 'og:description' => $s['og_description'] ?? '',
        'og:image' => $s['og_image'] ?? '',
    );
    foreach ($tags as $p => $v) if ($v !== '') echo '<meta property="' . esc_attr($p) . '" content="' . esc_attr($v) . '" />' . "\n";
    $tw = array('twitter:title' => $s['twitter_title'] ?? '', 'twitter:description' => $s['twitter_description'] ?? '', 'twitter:image' => $s['twitter_image'] ?? '');
    if (array_filter($tw)) echo '<meta name="twitter:card" content="summary_large_image" />' . "\n";
    foreach ($tw as $n => $v) if ($v !== '') echo '<meta name="' . esc_attr($n) . '" content="' . esc_attr($v) . '" />' . "\n";
}

/* Custom robots.txt. */
add_filter('robots_txt', 'falcon_seo_robots_filter', 10, 2);
function falcon_seo_robots_filter($output, $public) {
    $custom = get_option('falcon_seo_robots', '');
    return $custom !== '' ? $custom : $output;
}

/* Log real 404s into a ring buffer (feeds the redirect manager). */
add_action('template_redirect', 'falcon_seo_log_404');
function falcon_seo_log_404() {
    if (!is_404()) return;
    $uri = esc_url_raw($_SERVER['REQUEST_URI'] ?? '');
    if (!$uri) return;
    $log = get_option('falcon_seo_404log', array());
    $key = md5($uri);
    if (isset($log[$key])) { $log[$key]['count']++; $log[$key]['last'] = gmdate('c'); }
    else {
        $ref = isset($_SERVER['HTTP_REFERER']) ? esc_url_raw($_SERVER['HTTP_REFERER']) : '';
        $log[$key] = array('url' => $uri, 'count' => 1, 'referrer' => $ref, 'last' => gmdate('c'));
    }
    if (count($log) > 200) array_shift($log);
    update_option('falcon_seo_404log', $log, false);
}

/* Per-post hreflang alternate links. */
add_action('wp_head', 'falcon_seo_print_hreflang');
function falcon_seo_print_hreflang() {
    if (!is_singular()) return;
    $alts = get_post_meta(get_queried_object_id(), '_falcon_hreflang', true);
    if (!$alts || !is_array($alts)) return;
    foreach ($alts as $a) {
        if (empty($a['lang']) || empty($a['url'])) continue;
        echo '<link rel="alternate" hreflang="' . esc_attr($a['lang']) . '" href="' . esc_url($a['url']) . '" />' . "\n";
    }
}

/* Lazy-load toggle (overrides the WordPress default when set). */
add_filter('wp_lazy_loading_enabled', 'falcon_seo_lazy_filter');
function falcon_seo_lazy_filter($default) {
    $o = get_option('falcon_seo_lazyload', null);
    return $o === null ? $default : (bool) $o;
}

/* ============================================================
 * REST routes:  /wp-json/falcon/v1/*
 * ============================================================ */
add_action('rest_api_init', function () {
    $auth = 'falcon_seo_auth';

    // Onboarding: unauthenticated self-test (reports whether the auth header arrived).
    register_rest_route('falcon/v1', '/selftest', array('methods' => 'GET', 'permission_callback' => '__return_true', 'callback' => 'falcon_seo_rest_selftest'));

    register_rest_route('falcon/v1', '/site', array(
        'methods' => 'GET', 'permission_callback' => $auth, 'callback' => 'falcon_seo_rest_site',
    ));
    register_rest_route('falcon/v1', '/posts', array(
        'methods' => 'GET', 'permission_callback' => $auth, 'callback' => 'falcon_seo_rest_posts',
    ));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)', array(
        'methods' => 'GET', 'permission_callback' => $auth, 'callback' => 'falcon_seo_rest_post',
    ));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/seo', array(
        'methods' => 'POST', 'permission_callback' => $auth, 'callback' => 'falcon_seo_rest_stage_seo',
    ));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/link', array(
        'methods' => 'POST', 'permission_callback' => $auth, 'callback' => 'falcon_seo_rest_stage_link',
    ));
    register_rest_route('falcon/v1', '/pending', array(
        'methods' => 'GET', 'permission_callback' => $auth, 'callback' => 'falcon_seo_rest_pending',
    ));

    // --- Content management ---
    register_rest_route('falcon/v1', '/posts/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_post'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_post'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_post'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/revisions', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_revisions'));

    // --- Media ---
    register_rest_route('falcon/v1', '/media', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_media'));
    register_rest_route('falcon/v1', '/media/upload', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_upload_media'));
    register_rest_route('falcon/v1', '/media/(?P<id>\d+)/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_media'));

    // --- Taxonomies ---
    register_rest_route('falcon/v1', '/categories', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_categories'));
    register_rest_route('falcon/v1', '/categories/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_category'));
    register_rest_route('falcon/v1', '/tags', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_tags'));
    register_rest_route('falcon/v1', '/tags/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_tag'));

    // --- Menus ---
    register_rest_route('falcon/v1', '/menus', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_menus'));
    register_rest_route('falcon/v1', '/menus/(?P<id>\d+)', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_menu'));
    register_rest_route('falcon/v1', '/menus/(?P<id>\d+)/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_menu'));

    // --- Plugins & themes ---
    register_rest_route('falcon/v1', '/plugins', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_plugins'));
    register_rest_route('falcon/v1', '/plugins/toggle', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_toggle_plugin'));
    register_rest_route('falcon/v1', '/themes', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_themes'));
    register_rest_route('falcon/v1', '/themes/activate', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_activate_theme'));

    // --- Users ---
    register_rest_route('falcon/v1', '/users', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_users'));
    register_rest_route('falcon/v1', '/users/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_user'));
    register_rest_route('falcon/v1', '/users/(?P<id>\d+)/role', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_user_role'));

    // --- Settings ---
    register_rest_route('falcon/v1', '/settings', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_settings'));
    register_rest_route('falcon/v1', '/settings/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_settings'));

    // --- Comments ---
    register_rest_route('falcon/v1', '/comments', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_comments'));
    register_rest_route('falcon/v1', '/comments/(?P<id>\d+)/approve', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_approve_comment'));
    register_rest_route('falcon/v1', '/comments/(?P<id>\d+)/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_comment'));

    // --- SEO extended ---
    register_rest_route('falcon/v1', '/seo/bulk', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_bulk_seo'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/seo-score', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_seo_score'));

    // --- Pages ---
    register_rest_route('falcon/v1', '/pages', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_pages'));
    register_rest_route('falcon/v1', '/pages/homepage', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_homepage'));

    // --- Menus (build from scratch) ---
    register_rest_route('falcon/v1', '/menus/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_menu'));
    register_rest_route('falcon/v1', '/menus/(?P<id>\d+)/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_menu'));
    register_rest_route('falcon/v1', '/menus/locations', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_menu_locations'));
    register_rest_route('falcon/v1', '/menus/assign', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_assign_menu'));

    // --- Plugins (install/update/delete) ---
    register_rest_route('falcon/v1', '/plugins/install', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_install_plugin'));
    register_rest_route('falcon/v1', '/plugins/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_plugin'));
    register_rest_route('falcon/v1', '/plugins/update-all', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_all_plugins'));
    register_rest_route('falcon/v1', '/plugins/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_plugin'));
    register_rest_route('falcon/v1', '/updates', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_check_updates'));

    // --- Themes (install/update/delete/customize/files/child) ---
    register_rest_route('falcon/v1', '/themes/install', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_install_theme'));
    register_rest_route('falcon/v1', '/themes/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_theme'));
    register_rest_route('falcon/v1', '/themes/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_theme'));
    register_rest_route('falcon/v1', '/themes/customize', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_customize_theme'));
    // Theme files are read-only (view source) — writing/scaffolding theme code via the
    // API was removed: an authenticated caller must not be able to place arbitrary
    // executable PHP into wp-content/themes.
    register_rest_route('falcon/v1', '/themes/file', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_theme_file'));

    // --- Security ---
    register_rest_route('falcon/v1', '/security/scan', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_security_scan'));
    register_rest_route('falcon/v1', '/security/integrity', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_core_integrity'));
    register_rest_route('falcon/v1', '/security/users', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_audit_users'));
    register_rest_route('falcon/v1', '/security/harden', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_harden'));
    register_rest_route('falcon/v1', '/security/malware', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_malware_scan'));
    register_rest_route('falcon/v1', '/security/ssl', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_ssl_check'));
    register_rest_route('falcon/v1', '/security/vulnerabilities', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_vulnerabilities'));
    register_rest_route('falcon/v1', '/security/file-integrity', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_file_integrity'));
    register_rest_route('falcon/v1', '/security/permissions', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_permissions'));
    register_rest_route('falcon/v1', '/security/hardening', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_hardening'));
    register_rest_route('falcon/v1', '/security/logins', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_logins'));
    register_rest_route('falcon/v1', '/security/force-logout', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_force_logout'));
    register_rest_route('falcon/v1', '/security/suspicious', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_suspicious'));
    register_rest_route('falcon/v1', '/security/secrets', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_secrets'));
    register_rest_route('falcon/v1', '/security/mixed-content', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_mixed_content'));
    register_rest_route('falcon/v1', '/security/audit', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_sec_audit'));

    // --- WooCommerce ---
    register_rest_route('falcon/v1', '/woo/products', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_products'));
    register_rest_route('falcon/v1', '/woo/products/(?P<id>\d+)', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_product'));
    register_rest_route('falcon/v1', '/woo/products/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_create_product'));
    register_rest_route('falcon/v1', '/woo/products/(?P<id>\d+)/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_update_product'));
    register_rest_route('falcon/v1', '/woo/orders', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_orders'));
    register_rest_route('falcon/v1', '/woo/orders/(?P<id>\d+)', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_order'));
    register_rest_route('falcon/v1', '/woo/orders/(?P<id>\d+)/status', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_order_status'));
    register_rest_route('falcon/v1', '/woo/sales', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_sales'));

    // --- Content & SEO (advanced) ---
    register_rest_route('falcon/v1', '/content/find-replace', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_find_replace'));
    register_rest_route('falcon/v1', '/posts/schedule', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_schedule_post'));
    register_rest_route('falcon/v1', '/posts/scheduled', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_content_calendar'));
    register_rest_route('falcon/v1', '/media/missing-alt', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_missing_alt'));
    register_rest_route('falcon/v1', '/media/alt', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_alt'));
    register_rest_route('falcon/v1', '/redirects', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_redirects'));
    register_rest_route('falcon/v1', '/redirects/add', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_add_redirect'));
    register_rest_route('falcon/v1', '/redirects/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_delete_redirect'));
    register_rest_route('falcon/v1', '/robots', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_robots'));
    register_rest_route('falcon/v1', '/robots/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_update_robots'));
    register_rest_route('falcon/v1', '/sitemaps', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_sitemaps'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/schema', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_schema'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/schema/set', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_schema'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/social', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_social'));

    // --- Structure & builders ---
    register_rest_route('falcon/v1', '/blocks/build', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_build_gutenberg'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/elementor', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_elementor'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/elementor/set', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_elementor'));
    register_rest_route('falcon/v1', '/elementor/build', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_build_elementor'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/fields', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_fields'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/fields/set', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_field'));
    register_rest_route('falcon/v1', '/acf/field-groups', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_acf_list_groups'));
    register_rest_route('falcon/v1', '/acf/field-groups/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_acf_create_group'));
    register_rest_route('falcon/v1', '/widgets', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_widget_areas'));
    register_rest_route('falcon/v1', '/widgets/add', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_add_widget'));
    register_rest_route('falcon/v1', '/post-types', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_post_types'));
    register_rest_route('falcon/v1', '/page-templates', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_page_templates'));

    // --- Maintenance & safety ---
    register_rest_route('falcon/v1', '/backup/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_backup'));
    register_rest_route('falcon/v1', '/backup/list', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_backups'));
    register_rest_route('falcon/v1', '/db/status', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_db_status'));
    register_rest_route('falcon/v1', '/db/cleanup', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_db_cleanup'));
    register_rest_route('falcon/v1', '/cache/clear', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_clear_cache'));
    register_rest_route('falcon/v1', '/performance', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_performance'));
    register_rest_route('falcon/v1', '/links/broken', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_broken_links'));
    register_rest_route('falcon/v1', '/forms', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_forms'));
    register_rest_route('falcon/v1', '/forms/submissions', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_form_submissions'));
    register_rest_route('falcon/v1', '/cf7/forms', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cf7_list'));
    register_rest_route('falcon/v1', '/cf7/forms/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cf7_create'));
    register_rest_route('falcon/v1', '/cf7/forms/(?P<id>\d+)', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cf7_get'));
    register_rest_route('falcon/v1', '/cf7/forms/(?P<id>\d+)/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cf7_update'));
    register_rest_route('falcon/v1', '/cf7/forms/(?P<id>\d+)/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cf7_delete'));

    // --- Power tools (v1.11) ---
    register_rest_route('falcon/v1', '/rest/proxy', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_proxy'));
    register_rest_route('falcon/v1', '/db/query', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_db_query'));
    register_rest_route('falcon/v1', '/options/get', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_option'));
    register_rest_route('falcon/v1', '/options/set', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_option'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/duplicate', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_duplicate_post'));
    register_rest_route('falcon/v1', '/posts/bulk-delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_bulk_delete_posts'));
    register_rest_route('falcon/v1', '/cron', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cron'));
    register_rest_route('falcon/v1', '/cron/run', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cron_run'));
    register_rest_route('falcon/v1', '/cron/clear', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_cron_clear'));
    register_rest_route('falcon/v1', '/maintenance', array('methods'=>array('GET','POST'),'permission_callback'=>$auth,'callback'=>'falcon_seo_rest_maintenance'));
    register_rest_route('falcon/v1', '/comments/(?P<id>\d+)/reply', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_comment_reply'));
    register_rest_route('falcon/v1', '/comments/bulk', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_comment_bulk'));
    register_rest_route('falcon/v1', '/media/(?P<id>\d+)/replace', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_media_replace'));
    register_rest_route('falcon/v1', '/media/bulk-alt', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_bulk_alt'));
    register_rest_route('falcon/v1', '/block-patterns', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_block_patterns'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/insert-pattern', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_insert_pattern'));
    register_rest_route('falcon/v1', '/roles', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_roles'));
    register_rest_route('falcon/v1', '/roles/caps', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_role_caps'));
    register_rest_route('falcon/v1', '/roles/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_role_create'));
    register_rest_route('falcon/v1', '/rewrite/flush', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_flush_rewrite'));
    register_rest_route('falcon/v1', '/debug/log', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_debug_log'));
    register_rest_route('falcon/v1', '/woo/customers', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_customers'));
    register_rest_route('falcon/v1', '/woo/products/(?P<id>\d+)/variations', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_variations'));
    register_rest_route('falcon/v1', '/woo/variations/(?P<id>\d+)/update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_update_variation'));
    register_rest_route('falcon/v1', '/woo/shipping-zones', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_shipping_zones'));
    register_rest_route('falcon/v1', '/woo/tax-rates', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_tax_rates'));
    register_rest_route('falcon/v1', '/woo/webhooks', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_webhooks'));
    register_rest_route('falcon/v1', '/woo/webhooks/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_create_webhook'));

    // --- Themes (advanced / FSE) ---
    register_rest_route('falcon/v1', '/templates', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_templates'));
    register_rest_route('falcon/v1', '/templates/get', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_template'));
    register_rest_route('falcon/v1', '/templates/edit', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_edit_template'));
    register_rest_route('falcon/v1', '/global-styles', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_global_styles'));
    register_rest_route('falcon/v1', '/global-styles/set', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_global_styles'));

    // --- Custom plugins & code snippets ---
    // Custom-plugin scaffolding and PHP code snippets were removed for the same reason:
    // no REST-triggered arbitrary code should land in wp-content/plugins or mu-plugins.

    // --- WooCommerce (bulk & more) ---
    register_rest_route('falcon/v1', '/woo/products/bulk-update', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_bulk_update'));
    register_rest_route('falcon/v1', '/woo/products/bulk-create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_bulk_create'));
    register_rest_route('falcon/v1', '/woo/products/export', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_export'));
    register_rest_route('falcon/v1', '/woo/products/import', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_import'));
    register_rest_route('falcon/v1', '/woo/orders/bulk-status', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_bulk_order_status'));
    register_rest_route('falcon/v1', '/woo/coupons', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_list_coupons'));
    register_rest_route('falcon/v1', '/woo/coupons/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_create_coupon'));
    register_rest_route('falcon/v1', '/woo/coupons/delete', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_delete_coupon'));
    register_rest_route('falcon/v1', '/woo/categories', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_list_categories'));
    register_rest_route('falcon/v1', '/woo/categories/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_create_category'));
    register_rest_route('falcon/v1', '/woo/low-stock', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_low_stock'));
    register_rest_route('falcon/v1', '/woo/top-sellers', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_top_sellers'));

    // --- Images ---
    register_rest_route('falcon/v1', '/images/capabilities', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_image_caps'));
    register_rest_route('falcon/v1', '/images/webp', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_convert_webp'));
    register_rest_route('falcon/v1', '/images/optimize', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_optimize_images'));
    register_rest_route('falcon/v1', '/images/resize', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_resize_image'));
    register_rest_route('falcon/v1', '/images/regenerate', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_regen_thumbs'));
    register_rest_route('falcon/v1', '/images/lazyload', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_lazyload'));

    // --- SEO power ---
    register_rest_route('falcon/v1', '/seo/internal-links', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_internal_links'));
    register_rest_route('falcon/v1', '/seo/content-audit', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_content_audit'));
    register_rest_route('falcon/v1', '/seo/404-log', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_404log'));
    register_rest_route('falcon/v1', '/seo/404-log/clear', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_clear_404log'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/schema/template', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_schema_template'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/hreflang', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_get_hreflang'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/hreflang/set', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_hreflang'));

    // --- v1.6: content ops ---
    register_rest_route('falcon/v1', '/content/stale', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_stale_content'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/revisions/restore', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_restore_revision'));
    register_rest_route('falcon/v1', '/reusable-blocks', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_blocks'));
    register_rest_route('falcon/v1', '/reusable-blocks/create', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_block'));
    register_rest_route('falcon/v1', '/taxonomies', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_list_taxonomies'));
    register_rest_route('falcon/v1', '/taxonomies/term', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_create_term'));
    register_rest_route('falcon/v1', '/posts/(?P<id>\d+)/terms', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_assign_terms'));

    // --- v1.6: health & comms ---
    register_rest_route('falcon/v1', '/site-health', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_site_health'));
    register_rest_route('falcon/v1', '/accessibility', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_accessibility'));
    register_rest_route('falcon/v1', '/smtp/configure', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_configure_smtp'));
    register_rest_route('falcon/v1', '/smtp/test', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_test_email'));
    register_rest_route('falcon/v1', '/comments/purge-spam', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_purge_spam'));
    register_rest_route('falcon/v1', '/export/wxr', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_export_wxr'));

    // --- v1.6: WooCommerce reviews & refunds ---
    register_rest_route('falcon/v1', '/woo/reviews', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_reviews'));
    register_rest_route('falcon/v1', '/woo/reviews/(?P<id>\d+)/moderate', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_moderate_review'));
    register_rest_route('falcon/v1', '/woo/orders/(?P<id>\d+)/refund', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_wc_refund'));

    // --- v1.6: Google Indexing API ---
    register_rest_route('falcon/v1', '/google/service-account', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_set_google_sa'));
    register_rest_route('falcon/v1', '/google/index', array('methods'=>'POST','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_index_url'));
    register_rest_route('falcon/v1', '/google/index-status', array('methods'=>'GET','permission_callback'=>$auth,'callback'=>'falcon_seo_rest_index_status'));
});

function falcon_seo_rest_site() {
    $counts_post = wp_count_posts('post');
    $counts_page = wp_count_posts('page');
    global $wpdb;
    $pending = (int) $wpdb->get_var("SELECT COUNT(*) FROM " . falcon_seo_table() . " WHERE status='applied'");
    return rest_ensure_response(array(
        'plugin'         => 'TechShu SEO Bridge',
        'version'        => FALCON_SEO_VERSION,
        'site_name'      => get_bloginfo('name'),
        'site_url'       => get_site_url(),
        'wp_version'     => get_bloginfo('version'),
        'php_version'    => PHP_VERSION,
        'uploads_writable' => wp_is_writable(wp_get_upload_dir()['basedir']),
        'yoast_active'   => falcon_seo_yoast_active(),
        'woocommerce_active' => class_exists('WooCommerce'),
        'elementor_active' => defined('ELEMENTOR_VERSION'),
        'acf_active'     => function_exists('get_fields'),
        'published_posts'=> isset($counts_post->publish) ? (int) $counts_post->publish : 0,
        'published_pages'=> isset($counts_page->publish) ? (int) $counts_page->publish : 0,
        'recent_changes' => $pending,
        'approval_mode'  => 'auto',
    ));
}

function falcon_seo_rest_posts(WP_REST_Request $req) {
    $args = array(
        'post_type'      => $req->get_param('type') ? sanitize_key($req->get_param('type')) : 'post',
        'posts_per_page' => $req->get_param('per_page') ? min(100, max(1, (int) $req->get_param('per_page'))) : 20,
        'paged'          => $req->get_param('page') ? max(1, (int) $req->get_param('page')) : 1,
        's'              => $req->get_param('search') ? sanitize_text_field($req->get_param('search')) : '',
        'post_status'    => 'publish',
    );
    $q = new WP_Query($args);
    $rows = array();
    foreach ($q->posts as $p) {
        $rows[] = falcon_seo_post_dto($p, false);
    }
    return rest_ensure_response(array(
        'count' => count($rows),
        'total' => (int) $q->found_posts,
        'page'  => $args['paged'],
        'posts' => $rows,
    ));
}

function falcon_seo_rest_post(WP_REST_Request $req) {
    $post = get_post((int) $req['id']);
    if (!$post) {
        return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    }
    return rest_ensure_response(falcon_seo_post_dto($post, true));
}


function falcon_seo_rest_stage_seo(WP_REST_Request $req) {
    $post = get_post((int) $req['id']);
    if (!$post) {
        return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    }
    $body = $req->get_json_params();
    $payload = array();
    foreach (array('title', 'meta_description', 'focus_keyword') as $k) {
        if (isset($body[$k]) && $body[$k] !== '') {
            $payload[$k] = sanitize_text_field($body[$k]);
        }
    }
    if (empty($payload)) {
        return new WP_Error('falcon_empty', 'Provide title, meta_description and/or focus_keyword.', array('status' => 400));
    }
    $reason = isset($body['reason']) ? sanitize_text_field($body['reason']) : '';
    $before = falcon_seo_get_meta($post->ID);
    falcon_seo_apply($post->ID, 'seo_meta', $payload);
    falcon_seo_log($post->ID, 'seo_meta', array_merge($payload, array('before' => $before)), $reason);
    return rest_ensure_response(array(
        'applied' => true, 'post_id' => $post->ID, 'status' => 'live',
        'updated' => falcon_seo_get_meta($post->ID),
        'message' => 'SEO meta updated live on the site.',
    ));
}

function falcon_seo_rest_stage_link(WP_REST_Request $req) {
    $post = get_post((int) $req['id']);
    if (!$post) {
        return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    }
    $body = $req->get_json_params();
    $anchor = isset($body['anchor_text']) ? sanitize_text_field($body['anchor_text']) : '';
    $target = isset($body['target_url']) ? esc_url_raw($body['target_url']) : '';
    if (!$anchor || !$target) {
        return new WP_Error('falcon_empty', 'anchor_text and target_url are required.', array('status' => 400));
    }
    $reason = isset($body['reason']) ? sanitize_text_field($body['reason']) : '';
    $payload = array('anchor_text' => $anchor, 'target_url' => $target);
    falcon_seo_apply($post->ID, 'internal_link', $payload);
    falcon_seo_log($post->ID, 'internal_link', $payload, $reason);
    return rest_ensure_response(array(
        'applied' => true, 'post_id' => $post->ID, 'status' => 'live',
        'message' => 'Internal link added live to the post.',
    ));
}

function falcon_seo_rest_pending() {
    // History of recently applied changes (visibility only — no approval gate).
    global $wpdb;
    $rows = $wpdb->get_results("SELECT * FROM " . falcon_seo_table() . " WHERE status='applied' ORDER BY created_at DESC LIMIT 50", ARRAY_A);
    foreach ($rows as &$r) {
        $r['payload'] = json_decode($r['payload'], true);
        $r['post_title'] = get_the_title((int) $r['post_id']);
        $r['post_url'] = get_permalink((int) $r['post_id']);
    }
    return rest_ensure_response(array('count' => count($rows), 'recent_changes' => $rows));
}

/* ============================================================
 * Apply a change immediately (AI-controlled, no approval gate)
 * ============================================================ */
function falcon_seo_apply($post_id, $type, $payload) {
    $post_id = (int) $post_id;
    if ($type === 'seo_meta') {
        if (!empty($payload['title']))            update_post_meta($post_id, '_yoast_wpseo_title', $payload['title']);
        if (!empty($payload['meta_description'])) update_post_meta($post_id, '_yoast_wpseo_metadesc', $payload['meta_description']);
        if (!empty($payload['focus_keyword']))    update_post_meta($post_id, '_yoast_wpseo_focuskw', $payload['focus_keyword']);
        return true;
    }
    if ($type === 'internal_link') {
        $post = get_post($post_id);
        if (!$post) return false;
        $anchor = $payload['anchor_text'];
        $target = $payload['target_url'];
        $link = '<a href="' . esc_url($target) . '">' . esc_html($anchor) . '</a>';
        $content = $post->post_content;
        // Wrap first plain-text occurrence of the anchor; else append a "Related" line.
        if (strpos($content, $anchor) !== false && stripos($content, 'href') === false) {
            $content = preg_replace('/' . preg_quote($anchor, '/') . '/', $link, $content, 1);
        } else {
            $content .= "\n\n<p>Related: " . $link . "</p>";
        }
        wp_update_post(array('ID' => $post_id, 'post_content' => $content));
        return true;
    }
    return false;
}

/* Record an applied change to the history log (visibility only, no gating). */
function falcon_seo_log($post_id, $type, $payload, $reason) {
    global $wpdb;
    $wpdb->insert(falcon_seo_table(), array(
        'post_id'     => $post_id,
        'change_type' => $type,
        'payload'     => wp_json_encode($payload),
        'reason'      => $reason,
        'status'      => 'applied',
    ));
    return (int) $wpdb->insert_id;
}

/* ============================================================
 * Shared helpers for the extended endpoints
 * ============================================================ */
function falcon_seo_body(WP_REST_Request $req) { $b = $req->get_json_params(); return is_array($b) ? $b : array(); }
function falcon_seo_reason($b) { return isset($b['reason']) ? sanitize_text_field($b['reason']) : ''; }
function falcon_seo_default_author() {
    $a = get_users(array('role' => 'administrator', 'number' => 1, 'fields' => 'ID'));
    return !empty($a) ? (int) $a[0] : 1;
}
function falcon_seo_require_media() {
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';
}
function falcon_seo_term_ids($input, $tax) {
    $names = is_array($input) ? $input : array_map('trim', explode(',', (string) $input));
    $ids = array();
    foreach ($names as $n) {
        if ($n === '') continue;
        $t = term_exists($n, $tax);
        if (!$t) $t = wp_insert_term($n, $tax);
        if (!is_wp_error($t)) $ids[] = (int) (is_array($t) ? $t['term_id'] : $t);
    }
    return $ids;
}
function falcon_seo_set_featured($post_id, $url) {
    falcon_seo_require_media();
    $att = media_sideload_image(esc_url_raw($url), $post_id, null, 'id');
    if (!is_wp_error($att)) { set_post_thumbnail($post_id, $att); return (int) $att; }
    return 0;
}

/* ============================================================
 * Content management
 * ============================================================ */

// Truthy-parse the full_html flag (accepts bool, 1/0, "true"/"yes"/"on").
function falcon_seo_full_html($b) {
    if (!isset($b['full_html'])) return false;
    $v = $b['full_html'];
    if (is_bool($v)) return $v;
    if (is_numeric($v)) return (int) $v === 1;
    return in_array(strtolower((string) $v), array('1', 'true', 'yes', 'on'), true);
}

// Prepare post_content for wp_insert/update_post. full_html keeps it byte-exact (incl.
// <style>/<script>); otherwise it's run through wp_kses_post() like before. Callers must
// wrap the insert/update in kses_remove_filters()/kses_init_filters() (in try/finally) when
// $full is true, because WordPress's own content_save_pre KSES would re-strip it (no
// logged-in user here). wp_slash on BOTH paths: wp_insert_post wp_unslash()es internally, so
// without it backslashes in CSS/JS (content:"\2014", regex escapes) get corrupted.
function falcon_seo_prep_content($b, $full) {
    if (!isset($b['content'])) return '';
    return wp_slash($full ? $b['content'] : wp_kses_post($b['content']));
}

// List page templates the caller can pass as `template` to render a full-width landing page.
function falcon_seo_rest_page_templates() {
    $out = array();
    $theme = wp_get_theme();
    if ($theme && !is_wp_error($theme)) {
        foreach ($theme->get_page_templates(null, 'page') as $slug => $name) {
            $out[] = array('slug' => $slug, 'name' => $name);
        }
    }
    if (defined('ELEMENTOR_VERSION')) {
        $out[] = array('slug' => 'elementor_canvas', 'name' => 'Elementor Canvas — blank, no header/footer (best for landing pages)');
        $out[] = array('slug' => 'elementor_header_footer', 'name' => 'Elementor Full Width — keeps theme header/footer');
    }
    $is_block = function_exists('wp_is_block_theme') && wp_is_block_theme();
    return rest_ensure_response(array(
        'default'        => 'default',
        'is_block_theme' => $is_block,
        'count'          => count($out),
        'templates'      => $out,
        'note'           => 'Pass a slug as `template` in create_post/update_post. "default" = theme default. For a clean landing page choose a full-width/canvas template (e.g. elementor_canvas when Elementor is active). Block (FSE) themes assign templates differently — for those, set full_html=true and design the whole page in content.',
    ));
}

function falcon_seo_rest_create_post(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
    if ($title === '') return new WP_Error('falcon_empty', 'title is required.', array('status' => 400));
    $status = in_array(($b['status'] ?? 'draft'), array('draft','publish','pending','private'), true) ? $b['status'] : 'draft';
    // full_html preserves the caller's exact markup incl. <style>/<script> & inline CSS — needed for
    // self-contained landing pages, which wp_kses_post() would otherwise gut. The bridge token already
    // grants full admin (theme/file/snippet/plugin editing), so this exposes no new capability; the
    // default stays sanitized for safety.
    $full = falcon_seo_full_html($b);
    $arr = array(
        'post_title'   => $title,
        'post_content' => falcon_seo_prep_content($b, $full),
        'post_excerpt' => isset($b['excerpt']) ? sanitize_text_field($b['excerpt']) : '',
        'post_status'  => $status,
        'post_type'    => sanitize_key($b['type'] ?? 'post'),
        'post_author'  => falcon_seo_default_author(),
    );
    if (!empty($b['slug']))       $arr['post_name']     = sanitize_title($b['slug']);
    if (!empty($b['categories'])) $arr['post_category'] = falcon_seo_term_ids($b['categories'], 'category');
    if (!empty($b['tags']))       $arr['tags_input']    = is_array($b['tags']) ? $b['tags'] : array_map('trim', explode(',', $b['tags']));
    if ($full) kses_remove_filters();
    try {
        $id = wp_insert_post($arr, true);
    } finally {
        if ($full) kses_init_filters();  // always restore — a fatal mid-insert must not leave KSES off site-wide
    }
    if (is_wp_error($id)) return new WP_Error('falcon_err', $id->get_error_message(), array('status' => 400));
    if (!empty($b['template'])) update_post_meta($id, '_wp_page_template', sanitize_text_field($b['template']));
    if (!empty($b['featured_image_url'])) falcon_seo_set_featured($id, $b['featured_image_url']);
    falcon_seo_log($id, 'create_post', array('title' => $title, 'status' => $status, 'type' => $arr['post_type'], 'full_html' => $full), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'post_id' => $id, 'status' => get_post_status($id),
        'template' => !empty($b['template']) ? sanitize_text_field($b['template']) : null,
        'url' => get_permalink($id), 'edit_url' => admin_url("post.php?post={$id}&action=edit")));
}

function falcon_seo_rest_update_post(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $full = falcon_seo_full_html($b);
    $arr = array('ID' => $id);
    if (isset($b['title']))   $arr['post_title']   = sanitize_text_field($b['title']);
    if (isset($b['content'])) $arr['post_content'] = falcon_seo_prep_content($b, $full);
    if (isset($b['excerpt'])) $arr['post_excerpt'] = sanitize_text_field($b['excerpt']);
    if (isset($b['status']))  $arr['post_status']  = sanitize_key($b['status']);
    if (!empty($b['slug']))   $arr['post_name']    = sanitize_title($b['slug']);
    if ($full) kses_remove_filters();
    try {
        $r = wp_update_post($arr, true);
    } finally {
        if ($full) kses_init_filters();  // always restore even if the update fatals
    }
    if (is_wp_error($r)) return new WP_Error('falcon_err', $r->get_error_message(), array('status' => 400));
    if (!empty($b['template'])) update_post_meta($id, '_wp_page_template', sanitize_text_field($b['template']));
    if (isset($b['categories'])) wp_set_post_categories($id, falcon_seo_term_ids($b['categories'], 'category'));
    if (isset($b['tags']))       wp_set_post_tags($id, is_array($b['tags']) ? $b['tags'] : array_map('trim', explode(',', $b['tags'])));
    if (!empty($b['featured_image_url'])) falcon_seo_set_featured($id, $b['featured_image_url']);
    falcon_seo_log($id, 'update_post', array_intersect_key($arr, array_flip(array('post_title','post_status'))), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $id, 'url' => get_permalink($id)));
}

function falcon_seo_rest_delete_post(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $force = !empty($b['force']);
    $r = $force ? wp_delete_post($id, true) : wp_trash_post($id);
    falcon_seo_log($id, 'delete_post', array('permanent' => $force), falcon_seo_reason($b));
    return rest_ensure_response(array('deleted' => (bool) $r, 'permanent' => $force));
}

function falcon_seo_rest_revisions(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $revs = wp_get_post_revisions($id);
    $rows = array();
    foreach ($revs as $r) {
        $rows[] = array('id' => $r->ID, 'author' => get_the_author_meta('display_name', $r->post_author),
            'date' => $r->post_modified_gmt, 'title' => $r->post_title);
    }
    return rest_ensure_response(array('post_id' => $id, 'count' => count($rows), 'revisions' => $rows));
}

/* ============================================================
 * Media library
 * ============================================================ */
function falcon_seo_rest_list_media(WP_REST_Request $req) {
    $per = $req->get_param('per_page') ? min(100, max(1, (int) $req->get_param('per_page'))) : 20;
    $page = $req->get_param('page') ? max(1, (int) $req->get_param('page')) : 1;
    $q = new WP_Query(array('post_type' => 'attachment', 'post_status' => 'inherit', 'posts_per_page' => $per, 'paged' => $page));
    $rows = array();
    foreach ($q->posts as $p) {
        $rows[] = array('id' => $p->ID, 'title' => get_the_title($p), 'url' => wp_get_attachment_url($p->ID),
            'mime' => $p->post_mime_type, 'date' => $p->post_date_gmt,
            'alt' => get_post_meta($p->ID, '_wp_attachment_image_alt', true));
    }
    return rest_ensure_response(array('count' => count($rows), 'total' => (int) $q->found_posts, 'media' => $rows));
}

function falcon_seo_rest_upload_media(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $url = isset($b['url']) ? esc_url_raw($b['url']) : '';
    if (!$url) return new WP_Error('falcon_empty', 'url is required (image/file URL to import).', array('status' => 400));
    falcon_seo_require_media();
    $att = media_sideload_image($url, isset($b['post_id']) ? (int) $b['post_id'] : 0, isset($b['title']) ? sanitize_text_field($b['title']) : null, 'id');
    if (is_wp_error($att)) return new WP_Error('falcon_err', $att->get_error_message(), array('status' => 400));
    if (!empty($b['alt'])) update_post_meta($att, '_wp_attachment_image_alt', sanitize_text_field($b['alt']));
    falcon_seo_log($att, 'upload_media', array('source' => $url), falcon_seo_reason($b));
    return rest_ensure_response(array('uploaded' => true, 'media_id' => (int) $att, 'url' => wp_get_attachment_url($att)));
}

function falcon_seo_rest_delete_media(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $r = wp_delete_attachment($id, true);
    falcon_seo_log($id, 'delete_media', array(), '');
    return rest_ensure_response(array('deleted' => (bool) $r, 'media_id' => $id));
}

/* ============================================================
 * Categories & tags
 * ============================================================ */
function falcon_seo_term_dto($t) {
    return array('id' => $t->term_id, 'name' => $t->name, 'slug' => $t->slug, 'count' => $t->count, 'parent' => $t->parent);
}
function falcon_seo_rest_list_categories() {
    $rows = array_map('falcon_seo_term_dto', get_categories(array('hide_empty' => false)));
    return rest_ensure_response(array('count' => count($rows), 'categories' => $rows));
}
function falcon_seo_rest_create_category(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if ($name === '') return new WP_Error('falcon_empty', 'name is required.', array('status' => 400));
    $args = array();
    if (!empty($b['description'])) $args['description'] = sanitize_text_field($b['description']);
    if (!empty($b['parent']))      $args['parent'] = (int) $b['parent'];
    if (!empty($b['slug']))        $args['slug'] = sanitize_title($b['slug']);
    $t = wp_insert_term($name, 'category', $args);
    if (is_wp_error($t)) return new WP_Error('falcon_err', $t->get_error_message(), array('status' => 400));
    return rest_ensure_response(array('created' => true, 'id' => (int) $t['term_id'], 'name' => $name));
}
function falcon_seo_rest_list_tags() {
    $rows = array_map('falcon_seo_term_dto', get_tags(array('hide_empty' => false)));
    return rest_ensure_response(array('count' => count($rows), 'tags' => $rows));
}
function falcon_seo_rest_create_tag(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if ($name === '') return new WP_Error('falcon_empty', 'name is required.', array('status' => 400));
    $t = wp_insert_term($name, 'post_tag', !empty($b['description']) ? array('description' => sanitize_text_field($b['description'])) : array());
    if (is_wp_error($t)) return new WP_Error('falcon_err', $t->get_error_message(), array('status' => 400));
    return rest_ensure_response(array('created' => true, 'id' => (int) $t['term_id'], 'name' => $name));
}

/* ============================================================
 * Menus
 * ============================================================ */
function falcon_seo_rest_list_menus() {
    $rows = array();
    foreach (wp_get_nav_menus() as $m) $rows[] = array('id' => $m->term_id, 'name' => $m->name, 'slug' => $m->slug, 'count' => $m->count);
    return rest_ensure_response(array('count' => count($rows), 'menus' => $rows));
}
function falcon_seo_rest_get_menu(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $items = wp_get_nav_menu_items($id);
    $rows = array();
    if ($items) foreach ($items as $it) {
        $rows[] = array('id' => $it->ID, 'title' => $it->title, 'url' => $it->url, 'type' => $it->type,
            'object' => $it->object, 'parent' => (int) $it->menu_item_parent, 'order' => (int) $it->menu_order);
    }
    return rest_ensure_response(array('menu_id' => $id, 'count' => count($rows), 'items' => $rows));
}
function falcon_seo_rest_update_menu(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!wp_get_nav_menu_object($id)) return new WP_Error('falcon_not_found', 'Menu not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $added = array(); $removed = array();
    if (!empty($b['add']) && is_array($b['add'])) {
        foreach ($b['add'] as $item) {
            $new = wp_update_nav_menu_item($id, 0, array(
                'menu-item-title'  => sanitize_text_field($item['title'] ?? ''),
                'menu-item-url'    => esc_url_raw($item['url'] ?? ''),
                'menu-item-status' => 'publish',
            ));
            if (!is_wp_error($new)) $added[] = (int) $new;
        }
    }
    if (!empty($b['remove']) && is_array($b['remove'])) {
        foreach ($b['remove'] as $iid) { if (wp_delete_post((int) $iid, true)) $removed[] = (int) $iid; }
    }
    if (!empty($b['reorder']) && is_array($b['reorder'])) {
        $order = 1;
        foreach ($b['reorder'] as $iid) { wp_update_post(array('ID' => (int) $iid, 'menu_order' => $order++)); }
    }
    falcon_seo_log(0, 'update_menu', array('menu' => $id, 'added' => $added, 'removed' => $removed), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'menu_id' => $id, 'added' => $added, 'removed' => $removed));
}

/* ============================================================
 * Plugins & themes
 * ============================================================ */
function falcon_seo_rest_list_plugins() {
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
    $rows = array();
    foreach (get_plugins() as $file => $data) {
        $rows[] = array('plugin' => $file, 'name' => $data['Name'], 'version' => $data['Version'], 'active' => is_plugin_active($file));
    }
    return rest_ensure_response(array('count' => count($rows), 'plugins' => $rows));
}
function falcon_seo_rest_toggle_plugin(WP_REST_Request $req) {
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
    $b = falcon_seo_body($req);
    $file = isset($b['plugin']) ? sanitize_text_field($b['plugin']) : '';
    $active = !empty($b['active']);
    if (!$file) return new WP_Error('falcon_empty', 'plugin (file path) is required.', array('status' => 400));
    if ($active) {
        $r = activate_plugin($file);
        if (is_wp_error($r)) return new WP_Error('falcon_err', $r->get_error_message(), array('status' => 400));
    } else {
        deactivate_plugins($file);
    }
    falcon_seo_log(0, 'toggle_plugin', array('plugin' => $file, 'active' => $active), falcon_seo_reason($b));
    return rest_ensure_response(array('plugin' => $file, 'active' => is_plugin_active($file)));
}
function falcon_seo_rest_list_themes() {
    $current = get_stylesheet();
    $rows = array();
    foreach (wp_get_themes() as $slug => $t) {
        $rows[] = array('stylesheet' => $slug, 'name' => $t->get('Name'), 'version' => $t->get('Version'), 'active' => ($slug === $current));
    }
    return rest_ensure_response(array('count' => count($rows), 'themes' => $rows));
}
function falcon_seo_rest_activate_theme(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $slug = isset($b['stylesheet']) ? sanitize_text_field($b['stylesheet']) : '';
    if (!$slug || !wp_get_theme($slug)->exists()) return new WP_Error('falcon_err', 'Theme not found.', array('status' => 400));
    switch_theme($slug);
    falcon_seo_log(0, 'activate_theme', array('stylesheet' => $slug), falcon_seo_reason($b));
    return rest_ensure_response(array('active_theme' => get_stylesheet()));
}

/* ============================================================
 * Users
 * ============================================================ */
function falcon_seo_rest_list_users(WP_REST_Request $req) {
    $rows = array();
    foreach (get_users(array('number' => 100)) as $u) {
        $rows[] = array('id' => $u->ID, 'login' => $u->user_login, 'email' => $u->user_email,
            'name' => $u->display_name, 'roles' => $u->roles);
    }
    return rest_ensure_response(array('count' => count($rows), 'users' => $rows));
}
function falcon_seo_rest_create_user(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $login = isset($b['username']) ? sanitize_user($b['username']) : '';
    $email = isset($b['email']) ? sanitize_email($b['email']) : '';
    if (!$login || !$email) return new WP_Error('falcon_empty', 'username and email are required.', array('status' => 400));
    $role = isset($b['role']) ? sanitize_key($b['role']) : 'subscriber';
    if ($role === 'administrator') {
        return new WP_Error('falcon_forbidden', 'Creating administrator accounts through this API is not allowed. Promote the user manually in WP-admin if needed.', array('status' => 403));
    }
    $pass = !empty($b['password']) ? $b['password'] : wp_generate_password(20);
    $id = wp_insert_user(array('user_login' => $login, 'user_email' => $email, 'user_pass' => $pass, 'role' => $role));
    if (is_wp_error($id)) return new WP_Error('falcon_err', $id->get_error_message(), array('status' => 400));
    falcon_seo_log(0, 'create_user', array('login' => $login, 'role' => $role), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'user_id' => (int) $id, 'login' => $login));
}
function falcon_seo_rest_update_user_role(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $u = get_user_by('id', $id);
    if (!$u) return new WP_Error('falcon_not_found', 'User not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $role = isset($b['role']) ? sanitize_key($b['role']) : '';
    if (!$role || !get_role($role)) return new WP_Error('falcon_err', 'Invalid role.', array('status' => 400));
    if ($role === 'administrator') {
        return new WP_Error('falcon_forbidden', 'Promoting a user to administrator through this API is not allowed. Do it manually in WP-admin if needed.', array('status' => 403));
    }
    $u->set_role($role);
    falcon_seo_log(0, 'update_user_role', array('user_id' => $id, 'role' => $role), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'user_id' => $id, 'roles' => $u->roles));
}

/* ============================================================
 * Settings
 * ============================================================ */
function falcon_seo_settings_keys() {
    return array('blogname', 'blogdescription', 'admin_email', 'timezone_string', 'date_format',
        'time_format', 'start_of_week', 'posts_per_page', 'default_category', 'show_on_front', 'page_on_front');
}
function falcon_seo_rest_get_settings() {
    $out = array();
    foreach (falcon_seo_settings_keys() as $k) $out[$k] = get_option($k);
    return rest_ensure_response($out);
}
function falcon_seo_rest_update_settings(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $allowed = falcon_seo_settings_keys();
    $changed = array();
    foreach ($b as $k => $v) {
        if (!in_array($k, $allowed, true)) continue;
        if (in_array($k, array('posts_per_page', 'start_of_week', 'default_category', 'page_on_front'), true)) $v = (int) $v;
        elseif ($k === 'admin_email') $v = sanitize_email($v);
        else $v = sanitize_text_field($v);
        update_option($k, $v);
        $changed[$k] = $v;
    }
    falcon_seo_log(0, 'update_settings', $changed, falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'changed' => $changed));
}

/* ============================================================
 * Comments
 * ============================================================ */
function falcon_seo_rest_list_comments(WP_REST_Request $req) {
    $map = array('pending' => 'hold', 'approved' => 'approve', 'spam' => 'spam', 'trash' => 'trash', 'all' => 'all');
    $status = $req->get_param('status') ? sanitize_key($req->get_param('status')) : 'all';
    $wp_status = isset($map[$status]) ? $map[$status] : 'all';
    $comments = get_comments(array('status' => $wp_status, 'number' => $req->get_param('per_page') ? (int) $req->get_param('per_page') : 30));
    $rows = array();
    foreach ($comments as $c) {
        $rows[] = array('id' => $c->comment_ID, 'post_id' => (int) $c->comment_post_ID, 'post_title' => get_the_title($c->comment_post_ID),
            'author' => $c->comment_author, 'content' => wp_trim_words($c->comment_content, 40),
            'status' => wp_get_comment_status($c->comment_ID), 'date' => $c->comment_date_gmt);
    }
    return rest_ensure_response(array('count' => count($rows), 'comments' => $rows));
}
function falcon_seo_rest_approve_comment(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $r = wp_set_comment_status($id, 'approve');
    falcon_seo_log(0, 'approve_comment', array('comment_id' => $id), '');
    return rest_ensure_response(array('approved' => (bool) $r, 'comment_id' => $id));
}
function falcon_seo_rest_delete_comment(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $b = falcon_seo_body($req);
    $r = wp_delete_comment($id, !empty($b['force']));
    falcon_seo_log(0, 'delete_comment', array('comment_id' => $id, 'force' => !empty($b['force'])), '');
    return rest_ensure_response(array('deleted' => (bool) $r, 'comment_id' => $id));
}

/* ============================================================
 * SEO extended
 * ============================================================ */
function falcon_seo_rest_bulk_seo(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $items = isset($b['items']) && is_array($b['items']) ? $b['items'] : array();
    if (!$items) return new WP_Error('falcon_empty', 'items array is required.', array('status' => 400));
    $results = array();
    foreach ($items as $it) {
        $pid = (int) ($it['post_id'] ?? 0);
        if (!$pid || !get_post($pid)) { $results[] = array('post_id' => $pid, 'ok' => false, 'error' => 'not found'); continue; }
        $payload = array();
        foreach (array('title', 'meta_description', 'focus_keyword') as $k) {
            if (isset($it[$k]) && $it[$k] !== '') $payload[$k] = sanitize_text_field($it[$k]);
        }
        if (!$payload) { $results[] = array('post_id' => $pid, 'ok' => false, 'error' => 'no fields'); continue; }
        falcon_seo_apply($pid, 'seo_meta', $payload);
        falcon_seo_log($pid, 'seo_meta', $payload, isset($it['reason']) ? sanitize_text_field($it['reason']) : 'bulk');
        $results[] = array('post_id' => $pid, 'ok' => true, 'updated' => falcon_seo_get_meta($pid));
    }
    return rest_ensure_response(array('count' => count($results), 'results' => $results));
}
function falcon_seo_rest_seo_score(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $post = get_post($id);
    if (!$post) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $meta = falcon_seo_get_meta($id);
    $seo = (int) get_post_meta($id, '_yoast_wpseo_linkdex', true);          // 0-100 SEO score
    $read = (int) get_post_meta($id, '_yoast_wpseo_content_score', true);   // 0-100 readability
    $label = function ($s) { return $s >= 71 ? 'good' : ($s >= 41 ? 'ok' : ($s > 0 ? 'needs work' : 'not analyzed')); };
    $sugg = array();
    $md = $meta['meta_description'];
    if ($md === '') $sugg[] = 'Add a meta description.';
    elseif (strlen($md) < 120) $sugg[] = 'Meta description is short (<120 chars) — expand toward 150-160.';
    elseif (strlen($md) > 160) $sugg[] = 'Meta description is long (>160 chars) — it may be truncated in search.';
    $t = $meta['seo_title'] !== '' ? $meta['seo_title'] : get_the_title($post);
    if (strlen($t) > 60) $sugg[] = 'SEO title is long (>60 chars) — it may be truncated.';
    if ($meta['focus_keyword'] === '') $sugg[] = 'Set a focus keyword so Yoast can analyze the page.';
    elseif (stripos(get_the_title($post), $meta['focus_keyword']) === false) $sugg[] = 'Focus keyword is not in the post title.';
    if (str_word_count(wp_strip_all_tags($post->post_content)) < 300) $sugg[] = 'Content is thin (<300 words) — add more depth.';
    return rest_ensure_response(array(
        'post_id' => $id, 'seo_score' => $seo, 'seo_label' => $label($seo),
        'readability_score' => $read, 'readability_label' => $label($read),
        'focus_keyword' => $meta['focus_keyword'], 'suggestions' => $sugg,
    ));
}

/* ============================================================
 * Shared: sideload a remote image, return attachment id (0 on fail)
 * ============================================================ */
function falcon_seo_sideload($url) {
    falcon_seo_require_media();
    $att = media_sideload_image(esc_url_raw($url), 0, null, 'id');
    return is_wp_error($att) ? 0 : (int) $att;
}

/* ============================================================
 * Pages
 * ============================================================ */
function falcon_seo_rest_list_pages(WP_REST_Request $req) {
    $q = new WP_Query(array('post_type' => 'page', 'post_status' => array('publish', 'draft', 'private'),
        'posts_per_page' => 100, 'orderby' => 'menu_order title', 'order' => 'ASC'));
    $front = (int) get_option('page_on_front');
    $rows = array();
    foreach ($q->posts as $p) {
        $rows[] = array('id' => $p->ID, 'title' => get_the_title($p), 'status' => $p->post_status,
            'url' => get_permalink($p), 'is_front' => ($p->ID === $front), 'parent' => $p->post_parent);
    }
    return rest_ensure_response(array('count' => count($rows), 'pages' => $rows));
}
function falcon_seo_rest_set_homepage(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    if (!empty($b['show_latest_posts'])) {
        update_option('show_on_front', 'posts');
        falcon_seo_log(0, 'set_homepage', array('mode' => 'posts'), falcon_seo_reason($b));
        return rest_ensure_response(array('updated' => true, 'show_on_front' => 'posts'));
    }
    $pid = (int) ($b['page_id'] ?? 0);
    if (!$pid || get_post_type($pid) !== 'page') return new WP_Error('falcon_err', 'page_id (an existing page) is required.', array('status' => 400));
    update_option('show_on_front', 'page');
    update_option('page_on_front', $pid);
    if (!empty($b['posts_page_id'])) update_option('page_for_posts', (int) $b['posts_page_id']);
    falcon_seo_log(0, 'set_homepage', array('page_id' => $pid), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'show_on_front' => 'page', 'page_on_front' => $pid));
}

/* ============================================================
 * Menus — create / delete / locations / assign
 * ============================================================ */
function falcon_seo_rest_create_menu(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if ($name === '') return new WP_Error('falcon_empty', 'name is required.', array('status' => 400));
    $id = wp_create_nav_menu($name);
    if (is_wp_error($id)) return new WP_Error('falcon_err', $id->get_error_message(), array('status' => 400));
    if (!empty($b['items']) && is_array($b['items'])) {
        foreach ($b['items'] as $item) {
            wp_update_nav_menu_item($id, 0, array(
                'menu-item-title' => sanitize_text_field($item['title'] ?? ''),
                'menu-item-url' => esc_url_raw($item['url'] ?? ''),
                'menu-item-status' => 'publish'));
        }
    }
    if (!empty($b['location'])) {
        $locs = get_theme_mod('nav_menu_locations', array());
        $locs[sanitize_key($b['location'])] = (int) $id;
        set_theme_mod('nav_menu_locations', $locs);
    }
    falcon_seo_log(0, 'create_menu', array('menu_id' => (int) $id, 'name' => $name), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'menu_id' => (int) $id, 'name' => $name));
}
function falcon_seo_rest_delete_menu(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $r = wp_delete_nav_menu($id);
    falcon_seo_log(0, 'delete_menu', array('menu_id' => $id), '');
    return rest_ensure_response(array('deleted' => (bool) (!is_wp_error($r) && $r), 'menu_id' => $id));
}
function falcon_seo_rest_menu_locations() {
    $registered = get_registered_nav_menus();
    $assigned = get_nav_menu_locations();
    $rows = array();
    foreach ($registered as $loc => $desc) {
        $rows[] = array('location' => $loc, 'description' => $desc, 'menu_id' => isset($assigned[$loc]) ? (int) $assigned[$loc] : 0);
    }
    return rest_ensure_response(array('count' => count($rows), 'locations' => $rows));
}
function falcon_seo_rest_assign_menu(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $loc = isset($b['location']) ? sanitize_key($b['location']) : '';
    $mid = (int) ($b['menu_id'] ?? 0);
    if (!$loc) return new WP_Error('falcon_empty', 'location is required.', array('status' => 400));
    $locs = get_theme_mod('nav_menu_locations', array());
    if ($mid) $locs[$loc] = $mid; else unset($locs[$loc]);
    set_theme_mod('nav_menu_locations', $locs);
    falcon_seo_log(0, 'assign_menu', array('location' => $loc, 'menu_id' => $mid), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'location' => $loc, 'menu_id' => $mid));
}

/* ============================================================
 * Upgrader bootstrap (install / update plugins & themes)
 * ============================================================ */
function falcon_seo_load_upgrader() {
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/misc.php';
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
    require_once ABSPATH . 'wp-admin/includes/plugin-install.php';
    require_once ABSPATH . 'wp-admin/includes/theme.php';
    require_once ABSPATH . 'wp-admin/includes/theme-install.php';
    require_once ABSPATH . 'wp-admin/includes/update.php';
    require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';
}

/* ============================================================
 * Plugins — install / update / delete / check updates
 * ============================================================ */
// WordPress.org repository only — installing from an arbitrary zip_url was removed so a
// leaked token can't be used to plant unreviewed third-party code on the site.
function falcon_seo_rest_install_plugin(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $slug = isset($b['slug']) ? sanitize_key($b['slug']) : '';
    if (!$slug) return new WP_Error('falcon_empty', 'slug (the WordPress.org plugin slug) is required.', array('status' => 400));
    falcon_seo_load_upgrader();
    $api = plugins_api('plugin_information', array('slug' => $slug, 'fields' => array('sections' => false)));
    if (is_wp_error($api)) return new WP_Error('falcon_err', $api->get_error_message(), array('status' => 400));
    $upgrader = new Plugin_Upgrader(new Automatic_Upgrader_Skin());
    $result = $upgrader->install($api->download_link);
    if (is_wp_error($result)) return new WP_Error('falcon_err', $result->get_error_message(), array('status' => 400));
    if (!$result) return new WP_Error('falcon_err', 'Install failed (check filesystem permissions).', array('status' => 400));
    $file = $upgrader->plugin_info();
    $activated = false;
    if (!empty($b['activate']) && $file) { $a = activate_plugin($file); $activated = !is_wp_error($a); }
    falcon_seo_log(0, 'install_plugin', array('slug' => $slug, 'file' => $file, 'activated' => $activated), falcon_seo_reason($b));
    return rest_ensure_response(array('installed' => true, 'plugin' => $file, 'activated' => $activated));
}
function falcon_seo_rest_update_plugin(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $file = isset($b['plugin']) ? sanitize_text_field($b['plugin']) : '';
    if (!$file) return new WP_Error('falcon_empty', 'plugin (file path) is required.', array('status' => 400));
    falcon_seo_load_upgrader();
    wp_update_plugins();
    $upgrader = new Plugin_Upgrader(new Automatic_Upgrader_Skin());
    $result = $upgrader->upgrade($file);
    falcon_seo_log(0, 'update_plugin', array('plugin' => $file), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => ($result === true), 'plugin' => $file));
}
function falcon_seo_rest_update_all_plugins(WP_REST_Request $req) {
    falcon_seo_load_upgrader();
    wp_update_plugins();
    $files = array_keys(get_plugin_updates());
    if (!$files) return rest_ensure_response(array('updated' => array(), 'message' => 'All plugins already up to date.'));
    $upgrader = new Plugin_Upgrader(new Automatic_Upgrader_Skin());
    $results = $upgrader->bulk_upgrade($files);
    $ok = is_array($results) ? array_keys(array_filter($results)) : array();
    falcon_seo_log(0, 'update_all_plugins', array('plugins' => $files), '');
    return rest_ensure_response(array('attempted' => $files, 'updated' => $ok));
}
function falcon_seo_rest_delete_plugin(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $file = isset($b['plugin']) ? sanitize_text_field($b['plugin']) : '';
    if (!$file) return new WP_Error('falcon_empty', 'plugin (file path) is required.', array('status' => 400));
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    if (is_plugin_active($file)) deactivate_plugins($file);
    $r = delete_plugins(array($file));
    if (is_wp_error($r)) return new WP_Error('falcon_err', $r->get_error_message(), array('status' => 400));
    falcon_seo_log(0, 'delete_plugin', array('plugin' => $file), falcon_seo_reason($b));
    return rest_ensure_response(array('deleted' => ($r === true), 'plugin' => $file));
}
function falcon_seo_rest_check_updates() {
    falcon_seo_load_upgrader();
    wp_version_check(); wp_update_plugins(); wp_update_themes();
    $core = get_core_updates();
    $core_av = (!empty($core) && isset($core[0]->response) && $core[0]->response === 'upgrade') ? $core[0]->current : null;
    $plugins = array();
    foreach (get_plugin_updates() as $file => $p) {
        $plugins[] = array('plugin' => $file, 'name' => $p->Name, 'current' => $p->Version, 'new' => $p->update->new_version);
    }
    $themes = array();
    foreach (get_theme_updates() as $slug => $t) {
        $themes[] = array('stylesheet' => $slug, 'name' => $t->get('Name'), 'current' => $t->get('Version'), 'new' => $t->update['new_version']);
    }
    return rest_ensure_response(array(
        'core_update_available' => $core_av,
        'plugins_outdated' => count($plugins), 'plugins' => $plugins,
        'themes_outdated' => count($themes), 'themes' => $themes,
    ));
}

/* ============================================================
 * Themes — install / update / delete / customize / files / child
 * ============================================================ */
// WordPress.org repository only — see falcon_seo_rest_install_plugin for why zip_url was removed.
function falcon_seo_rest_install_theme(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $slug = isset($b['slug']) ? sanitize_key($b['slug']) : '';
    if (!$slug) return new WP_Error('falcon_empty', 'slug (the WordPress.org theme slug) is required.', array('status' => 400));
    falcon_seo_load_upgrader();
    $api = themes_api('theme_information', array('slug' => $slug, 'fields' => array('sections' => false)));
    if (is_wp_error($api)) return new WP_Error('falcon_err', $api->get_error_message(), array('status' => 400));
    $upgrader = new Theme_Upgrader(new Automatic_Upgrader_Skin());
    $result = $upgrader->install($api->download_link);
    if (is_wp_error($result)) return new WP_Error('falcon_err', $result->get_error_message(), array('status' => 400));
    if (!$result) return new WP_Error('falcon_err', 'Install failed (check filesystem permissions).', array('status' => 400));
    $info = $upgrader->theme_info();
    $stylesheet = $info ? $info->get_stylesheet() : $slug;
    $activated = false;
    if (!empty($b['activate']) && $stylesheet) { switch_theme($stylesheet); $activated = true; }
    falcon_seo_log(0, 'install_theme', array('slug' => $slug, 'stylesheet' => $stylesheet, 'activated' => $activated), falcon_seo_reason($b));
    return rest_ensure_response(array('installed' => true, 'stylesheet' => $stylesheet, 'activated' => $activated));
}
function falcon_seo_rest_update_theme(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $slug = isset($b['stylesheet']) ? sanitize_text_field($b['stylesheet']) : '';
    if (!$slug) return new WP_Error('falcon_empty', 'stylesheet is required.', array('status' => 400));
    falcon_seo_load_upgrader();
    wp_update_themes();
    $upgrader = new Theme_Upgrader(new Automatic_Upgrader_Skin());
    $result = $upgrader->upgrade($slug);
    falcon_seo_log(0, 'update_theme', array('stylesheet' => $slug), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => ($result === true), 'stylesheet' => $slug));
}
function falcon_seo_rest_delete_theme(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $slug = isset($b['stylesheet']) ? sanitize_text_field($b['stylesheet']) : '';
    if (!$slug) return new WP_Error('falcon_empty', 'stylesheet is required.', array('status' => 400));
    if ($slug === get_stylesheet()) return new WP_Error('falcon_err', 'Cannot delete the active theme.', array('status' => 400));
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/theme.php';
    $r = delete_theme($slug);
    if (is_wp_error($r)) return new WP_Error('falcon_err', $r->get_error_message(), array('status' => 400));
    falcon_seo_log(0, 'delete_theme', array('stylesheet' => $slug), falcon_seo_reason($b));
    return rest_ensure_response(array('deleted' => ($r === true), 'stylesheet' => $slug));
}
function falcon_seo_rest_customize_theme(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $changed = array();
    if (!empty($b['logo_url'])) {
        $att = falcon_seo_sideload($b['logo_url']);
        if ($att) { set_theme_mod('custom_logo', $att); $changed['custom_logo'] = $att; }
    }
    if (!empty($b['site_icon_url'])) {
        $icon = falcon_seo_sideload($b['site_icon_url']);
        if ($icon) { update_option('site_icon', $icon); $changed['site_icon'] = $icon; }
    }
    foreach (array('background_color', 'header_textcolor') as $ck) {
        if (isset($b[$ck])) { set_theme_mod($ck, sanitize_hex_color_no_hash($b[$ck])); $changed[$ck] = $b[$ck]; }
    }
    if (!empty($b['mods']) && is_array($b['mods'])) {
        foreach ($b['mods'] as $k => $v) {
            set_theme_mod(sanitize_key($k), is_string($v) ? sanitize_text_field($v) : $v);
            $changed[$k] = $v;
        }
    }
    falcon_seo_log(0, 'customize_theme', $changed, falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'changed' => $changed));
}
function falcon_seo_theme_path($stylesheet, $rel) {
    $theme = wp_get_theme($stylesheet);
    if (!$theme->exists()) return null;
    $base = wp_normalize_path($theme->get_stylesheet_directory());
    $full = wp_normalize_path($base . '/' . ltrim($rel, '/'));
    if (strpos($full, $base) !== 0) return null; // block path traversal
    return $full;
}
function falcon_seo_rest_get_theme_file(WP_REST_Request $req) {
    $stylesheet = $req->get_param('stylesheet') ? sanitize_text_field($req->get_param('stylesheet')) : get_stylesheet();
    $rel = $req->get_param('file') ? sanitize_text_field($req->get_param('file')) : '';
    $theme = wp_get_theme($stylesheet);
    if (!$theme->exists()) return new WP_Error('falcon_err', 'Theme not found.', array('status' => 400));
    if (!$rel) {
        $files = array_keys($theme->get_files(array('php', 'css', 'js', 'html'), 2));
        return rest_ensure_response(array('stylesheet' => $stylesheet, 'files' => $files));
    }
    $path = falcon_seo_theme_path($stylesheet, $rel);
    if (!$path || !file_exists($path)) return new WP_Error('falcon_not_found', 'File not found.', array('status' => 404));
    return rest_ensure_response(array('stylesheet' => $stylesheet, 'file' => $rel, 'content' => file_get_contents($path)));
}
/* ============================================================
 * Security — scan / integrity / users / harden / malware / ssl
 * ============================================================ */
function falcon_seo_rest_security_scan() {
    falcon_seo_load_upgrader();
    wp_version_check(); wp_update_plugins(); wp_update_themes();
    $issues = array(); $score = 100;
    $core = get_core_updates();
    if (!empty($core) && isset($core[0]->response) && $core[0]->response === 'upgrade') {
        $issues[] = array('level' => 'high', 'area' => 'core', 'message' => 'WordPress core update available: ' . $core[0]->current);
        $score -= 15;
    }
    $pu = get_plugin_updates();
    if ($pu) { $issues[] = array('level' => 'high', 'area' => 'plugins', 'message' => count($pu) . ' plugin(s) need updating.'); $score -= min(20, count($pu) * 5); }
    $tu = get_theme_updates();
    if ($tu) { $issues[] = array('level' => 'medium', 'area' => 'themes', 'message' => count($tu) . ' theme(s) need updating.'); $score -= min(10, count($tu) * 5); }
    if (!defined('DISALLOW_FILE_EDIT') || !DISALLOW_FILE_EDIT) {
        $issues[] = array('level' => 'medium', 'area' => 'hardening', 'message' => 'Built-in theme/plugin file editor is enabled (DISALLOW_FILE_EDIT not set).'); $score -= 5;
    }
    if (defined('WP_DEBUG') && WP_DEBUG) { $issues[] = array('level' => 'medium', 'area' => 'config', 'message' => 'WP_DEBUG is enabled on a live site.'); $score -= 5; }
    if (strpos(get_option('siteurl'), 'https://') !== 0) { $issues[] = array('level' => 'high', 'area' => 'ssl', 'message' => 'Site URL is not HTTPS.'); $score -= 15; }
    if (get_user_by('login', 'admin')) { $issues[] = array('level' => 'high', 'area' => 'users', 'message' => "Default 'admin' username exists — rename it."); $score -= 10; }
    if (apply_filters('xmlrpc_enabled', true)) { $issues[] = array('level' => 'low', 'area' => 'hardening', 'message' => 'XML-RPC is enabled (common brute-force / DDoS vector).'); $score -= 3; }
    if (version_compare(PHP_VERSION, '8.0', '<')) { $issues[] = array('level' => 'medium', 'area' => 'config', 'message' => 'PHP ' . PHP_VERSION . ' is outdated — upgrade to 8.1+.'); $score -= 5; }
    $admins = get_users(array('role' => 'administrator', 'fields' => array('user_login')));
    $score = max(0, $score);
    return rest_ensure_response(array(
        'score' => $score, 'grade' => ($score >= 85 ? 'A' : ($score >= 70 ? 'B' : ($score >= 50 ? 'C' : 'D'))),
        'issue_count' => count($issues), 'issues' => $issues,
        'admins' => array_map(function ($u) { return $u->user_login; }, $admins),
        'php_version' => PHP_VERSION, 'wp_version' => get_bloginfo('version'),
    ));
}
function falcon_seo_rest_core_integrity() {
    require_once ABSPATH . 'wp-admin/includes/update.php';
    global $wp_version, $wp_local_package;
    $locale = !empty($wp_local_package) ? $wp_local_package : 'en_US';
    $checksums = get_core_checksums($wp_version, $locale);
    if (!$checksums) return new WP_Error('falcon_err', 'Could not fetch official checksums from WordPress.org.', array('status' => 400));
    $modified = array(); $missing = array(); $checked = 0;
    foreach ($checksums as $file => $md5) {
        if (strpos($file, 'wp-content/') === 0) continue; // skip user content
        $path = ABSPATH . $file;
        $checked++;
        if (!file_exists($path)) { $missing[] = $file; continue; }
        if (md5_file($path) !== $md5) $modified[] = $file;
        if (count($modified) + count($missing) > 200) break;
    }
    return rest_ensure_response(array(
        'wp_version' => $wp_version, 'files_checked' => $checked,
        'modified_count' => count($modified), 'modified' => $modified,
        'missing_count' => count($missing), 'missing' => $missing,
        'clean' => (empty($modified) && empty($missing)),
    ));
}
function falcon_seo_rest_audit_users() {
    $rows = array(); $admins = 0;
    foreach (get_users(array('number' => 500)) as $u) {
        $is_admin = in_array('administrator', $u->roles, true);
        if ($is_admin) $admins++;
        $rows[] = array('id' => $u->ID, 'login' => $u->user_login, 'email' => $u->user_email, 'roles' => $u->roles,
            'is_admin' => $is_admin, 'is_default_admin' => ($u->user_login === 'admin'), 'registered' => $u->user_registered);
    }
    $warnings = array();
    if ($admins > 3) $warnings[] = $admins . ' administrator accounts — keep this to a minimum.';
    foreach ($rows as $r) if ($r['is_default_admin']) $warnings[] = "User 'admin' exists — rename it to reduce brute-force risk.";
    return rest_ensure_response(array('count' => count($rows), 'admin_count' => $admins, 'users' => $rows, 'warnings' => $warnings));
}
// Applied live from the saved option on every load (see the hooks below) — no file is
// written to mu-plugins, so hardening only applies while this plugin is active, and
// there's nothing left running (or to clean up) if it's ever deactivated.
function falcon_seo_rest_harden(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $keys = array('disable_file_edit', 'disable_xmlrpc', 'hide_wp_version', 'security_headers', 'hide_login_errors', 'block_user_enum');
    $flags = array();
    foreach ($keys as $k) $flags[$k] = !empty($b[$k]);
    if (!empty($b['block_user_enumeration'])) $flags['block_user_enum'] = true;
    if (!empty($b['all'])) foreach ($keys as $k) $flags[$k] = true;
    update_option('falcon_seo_hardening', $flags);
    falcon_seo_log(0, 'harden_site', $flags, falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'applied' => $flags,
        'note' => 'Hardening applied live while this plugin is active. Re-run with all=false (or individual flags false) to relax.'));
}
function falcon_seo_hardening_flag($key) {
    $flags = get_option('falcon_seo_hardening');
    return is_array($flags) && !empty($flags[$key]);
}
if (falcon_seo_hardening_flag('disable_file_edit') && !defined('DISALLOW_FILE_EDIT')) {
    define('DISALLOW_FILE_EDIT', true);
}
add_filter('xmlrpc_enabled', function ($enabled) {
    return falcon_seo_hardening_flag('disable_xmlrpc') ? false : $enabled;
});
add_action('wp_head', function () {
    if (falcon_seo_hardening_flag('hide_wp_version')) remove_action('wp_head', 'wp_generator');
}, 1);
add_filter('the_generator', function ($gen) {
    return falcon_seo_hardening_flag('hide_wp_version') ? '' : $gen;
});
add_filter('login_errors', function ($error) {
    return falcon_seo_hardening_flag('hide_login_errors') ? 'Invalid credentials.' : $error;
});
add_action('init', function () {
    if (falcon_seo_hardening_flag('block_user_enum') && !is_admin() && isset($_GET['author'])) {
        wp_safe_redirect(home_url(), 301); exit;
    }
});
add_action('send_headers', function () {
    if (!falcon_seo_hardening_flag('security_headers')) return;
    header('X-Frame-Options: SAMEORIGIN');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: strict-origin-when-cross-origin');
    header('X-XSS-Protection: 1; mode=block');
});
function falcon_seo_rest_malware_scan(WP_REST_Request $req) {
    $patterns = array('eval\s*\(', 'base64_decode\s*\(', 'gzinflate\s*\(', 'str_rot13\s*\(', 'shell_exec\s*\(',
        'system\s*\(', 'passthru\s*\(', 'exec\s*\(', 'assert\s*\(', '\$_(POST|GET|REQUEST|COOKIE)\s*\[[^\]]*\]\s*\(',
        'FilesMan', 'c99shell', 'r57shell', 'WSOshell');
    $re = '/' . implode('|', $patterns) . '/i';
    $upload = wp_get_upload_dir();
    $roots = array('uploads' => $upload['basedir'], 'themes' => get_theme_root(), 'plugins' => WP_PLUGIN_DIR);
    $hits = array(); $scanned = 0; $cap = 8000;
    foreach ($roots as $label => $root) {
        if (!is_dir($root)) continue;
        $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS));
        foreach ($it as $f) {
            if ($scanned >= $cap) break 2;
            $ext = strtolower($f->getExtension());
            $name = $f->getPathname();
            if ($label === 'uploads' && in_array($ext, array('php', 'phtml', 'php5'), true)) {
                $hits[] = array('file' => str_replace(ABSPATH, '', $name), 'reason' => 'Executable PHP file inside /uploads', 'area' => $label);
                continue;
            }
            if (!in_array($ext, array('php', 'phtml', 'inc'), true)) continue;
            $scanned++;
            if ($f->getSize() > 2000000) continue;
            $content = @file_get_contents($name);
            if ($content && preg_match($re, $content, $m)) {
                $hits[] = array('file' => str_replace(ABSPATH, '', $name), 'reason' => 'Suspicious pattern: ' . substr($m[0], 0, 40), 'area' => $label);
            }
            if (count($hits) > 200) break 2;
        }
    }
    return rest_ensure_response(array(
        'files_scanned' => $scanned, 'hit_count' => count($hits), 'hits' => $hits,
        'note' => 'Heuristic scan — these functions also appear in legitimate plugins, so review hits in context. PHP files inside /uploads are the strongest red flag.',
    ));
}
function falcon_seo_rest_ssl_check() {
    $site = get_option('siteurl'); $home = get_option('home');
    $report = array(
        'site_url_https' => strpos($site, 'https://') === 0,
        'home_url_https' => strpos($home, 'https://') === 0,
        'is_ssl_request' => is_ssl(),
        'force_ssl_admin' => (defined('FORCE_SSL_ADMIN') && FORCE_SSL_ADMIN),
    );
    $report['ok'] = $report['site_url_https'] && $report['home_url_https'];
    $report['suggestions'] = array();
    if (!$report['site_url_https']) $report['suggestions'][] = 'Set Site URL to https:// in Settings → General.';
    if (!$report['force_ssl_admin']) $report['suggestions'][] = "Add define('FORCE_SSL_ADMIN', true); to wp-config.php to force SSL in the admin area.";
    return rest_ensure_response($report);
}

/* ============================================================
 * WooCommerce
 * ============================================================ */
function falcon_seo_woo_guard() {
    if (!class_exists('WooCommerce')) return new WP_Error('falcon_woo', 'WooCommerce is not active on this site.', array('status' => 400));
    return true;
}
function falcon_seo_product_dto($p) {
    return array('id' => $p->get_id(), 'name' => $p->get_name(), 'sku' => $p->get_sku(), 'type' => $p->get_type(),
        'price' => $p->get_price(), 'regular_price' => $p->get_regular_price(), 'sale_price' => $p->get_sale_price(),
        'stock_status' => $p->get_stock_status(), 'stock_qty' => $p->get_stock_quantity(), 'status' => $p->get_status(),
        'url' => get_permalink($p->get_id()));
}
function falcon_seo_rest_wc_products(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $args = array('limit' => $req->get_param('per_page') ? (int) $req->get_param('per_page') : 20,
        'page' => $req->get_param('page') ? (int) $req->get_param('page') : 1);
    if ($req->get_param('search')) $args['s'] = sanitize_text_field($req->get_param('search'));
    $rows = array();
    foreach (wc_get_products($args) as $p) $rows[] = falcon_seo_product_dto($p);
    return rest_ensure_response(array('count' => count($rows), 'products' => $rows));
}
function falcon_seo_rest_wc_product(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $p = wc_get_product((int) $req['id']);
    if (!$p) return new WP_Error('falcon_not_found', 'Product not found.', array('status' => 404));
    $dto = falcon_seo_product_dto($p);
    $dto['description'] = $p->get_description();
    $dto['short_description'] = $p->get_short_description();
    $dto['categories'] = wp_get_post_terms($p->get_id(), 'product_cat', array('fields' => 'names'));
    return rest_ensure_response($dto);
}
function falcon_seo_rest_wc_create_product(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if ($name === '') return new WP_Error('falcon_empty', 'name is required.', array('status' => 400));
    $p = new WC_Product_Simple();
    $p->set_name($name);
    if (isset($b['regular_price'])) $p->set_regular_price((string) $b['regular_price']);
    if (isset($b['sale_price'])) $p->set_sale_price((string) $b['sale_price']);
    if (isset($b['description'])) $p->set_description(wp_kses_post($b['description']));
    if (isset($b['short_description'])) $p->set_short_description(wp_kses_post($b['short_description']));
    if (isset($b['sku'])) $p->set_sku(sanitize_text_field($b['sku']));
    if (isset($b['stock_quantity'])) { $p->set_manage_stock(true); $p->set_stock_quantity((int) $b['stock_quantity']); }
    $p->set_status(in_array(($b['status'] ?? 'publish'), array('publish', 'draft', 'pending', 'private'), true) ? $b['status'] : 'publish');
    $id = $p->save();
    if (!empty($b['categories'])) wp_set_object_terms($id, is_array($b['categories']) ? $b['categories'] : array_map('trim', explode(',', $b['categories'])), 'product_cat');
    if (!empty($b['image_url'])) { $att = falcon_seo_sideload($b['image_url']); if ($att) set_post_thumbnail($id, $att); }
    falcon_seo_log($id, 'wc_create_product', array('name' => $name), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'product_id' => $id, 'url' => get_permalink($id)));
}
function falcon_seo_rest_wc_update_product(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $p = wc_get_product((int) $req['id']);
    if (!$p) return new WP_Error('falcon_not_found', 'Product not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    if (isset($b['name'])) $p->set_name(sanitize_text_field($b['name']));
    if (isset($b['regular_price'])) $p->set_regular_price((string) $b['regular_price']);
    if (isset($b['sale_price'])) $p->set_sale_price((string) $b['sale_price']);
    if (isset($b['description'])) $p->set_description(wp_kses_post($b['description']));
    if (isset($b['stock_quantity'])) { $p->set_manage_stock(true); $p->set_stock_quantity((int) $b['stock_quantity']); }
    if (isset($b['stock_status'])) $p->set_stock_status(sanitize_key($b['stock_status']));
    if (isset($b['status'])) $p->set_status(sanitize_key($b['status']));
    $id = $p->save();
    falcon_seo_log($id, 'wc_update_product', array('product_id' => $id), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'product_id' => $id));
}
function falcon_seo_rest_wc_orders(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $args = array('limit' => $req->get_param('per_page') ? (int) $req->get_param('per_page') : 20,
        'page' => $req->get_param('page') ? (int) $req->get_param('page') : 1, 'orderby' => 'date', 'order' => 'DESC');
    if ($req->get_param('status')) $args['status'] = sanitize_key($req->get_param('status'));
    $rows = array();
    foreach (wc_get_orders($args) as $o) {
        $rows[] = array('id' => $o->get_id(), 'number' => $o->get_order_number(), 'status' => $o->get_status(),
            'total' => $o->get_total(), 'currency' => $o->get_currency(),
            'customer' => trim($o->get_billing_first_name() . ' ' . $o->get_billing_last_name()),
            'email' => $o->get_billing_email(), 'date' => $o->get_date_created() ? $o->get_date_created()->date('c') : null,
            'items' => $o->get_item_count());
    }
    return rest_ensure_response(array('count' => count($rows), 'orders' => $rows));
}
function falcon_seo_rest_wc_order(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $o = wc_get_order((int) $req['id']);
    if (!$o) return new WP_Error('falcon_not_found', 'Order not found.', array('status' => 404));
    $items = array();
    foreach ($o->get_items() as $it) $items[] = array('name' => $it->get_name(), 'qty' => $it->get_quantity(), 'total' => $it->get_total());
    return rest_ensure_response(array('id' => $o->get_id(), 'status' => $o->get_status(), 'total' => $o->get_total(),
        'currency' => $o->get_currency(), 'customer' => trim($o->get_billing_first_name() . ' ' . $o->get_billing_last_name()),
        'email' => $o->get_billing_email(), 'phone' => $o->get_billing_phone(), 'address' => $o->get_formatted_billing_address(),
        'items' => $items, 'payment_method' => $o->get_payment_method_title(),
        'date' => $o->get_date_created() ? $o->get_date_created()->date('c') : null));
}
function falcon_seo_rest_wc_order_status(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $o = wc_get_order((int) $req['id']);
    if (!$o) return new WP_Error('falcon_not_found', 'Order not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $status = isset($b['status']) ? sanitize_key($b['status']) : '';
    if (!$status) return new WP_Error('falcon_empty', 'status is required (e.g. processing, completed, cancelled, refunded).', array('status' => 400));
    $o->update_status($status, isset($b['note']) ? sanitize_text_field($b['note']) : '');
    falcon_seo_log($o->get_id(), 'wc_order_status', array('status' => $status), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'order_id' => $o->get_id(), 'status' => $o->get_status()));
}
function falcon_seo_rest_wc_sales(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $days = $req->get_param('days') ? max(1, min(365, (int) $req->get_param('days'))) : 30;
    $orders = wc_get_orders(array('limit' => -1, 'status' => array('completed', 'processing'),
        'date_created' => '>' . (time() - $days * 86400)));
    $total = 0; $count = 0; $items = 0;
    foreach ($orders as $o) { $total += (float) $o->get_total(); $count++; $items += $o->get_item_count(); }
    return rest_ensure_response(array('period_days' => $days, 'orders' => $count, 'items_sold' => $items,
        'revenue' => round($total, 2), 'currency' => get_woocommerce_currency(),
        'avg_order_value' => $count ? round($total / $count, 2) : 0));
}

/* ============================================================
 * Onboarding self-test (no auth) — diagnose header stripping
 * ============================================================ */
// Deliberately public + minimal: only enough to diagnose "is the site reachable and
// does the Authorization header arrive", never anything a site fingerprinter could use
// (PHP version, filesystem state, active-plugin map). Those live behind auth on /site.
function falcon_seo_rest_selftest(WP_REST_Request $req) {
    return rest_ensure_response(array(
        'plugin' => 'TechShu SEO Bridge', 'version' => FALCON_SEO_VERSION, 'rest_ok' => true,
        'auth_header_received' => (falcon_seo_get_auth_header() !== ''),
    ));
}

/* ============================================================
 * Content & SEO (advanced)
 * ============================================================ */
function falcon_seo_rest_find_replace(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $find = isset($b['find']) ? (string) $b['find'] : '';
    if ($find === '') return new WP_Error('falcon_empty', 'find is required.', array('status' => 400));
    $replace = isset($b['replace']) ? (string) $b['replace'] : '';
    $dry = !empty($b['dry_run']);
    $also_title = !empty($b['include_title']);
    $types = !empty($b['post_types']) ? (is_array($b['post_types']) ? $b['post_types'] : array($b['post_types'])) : array('post', 'page');
    $types = array_map('sanitize_key', $types);
    if (!$dry) falcon_seo_quick_backup('before find-replace: ' . $find);
    $q = new WP_Query(array('post_type' => $types, 'post_status' => 'any', 'posts_per_page' => -1, 'fields' => 'ids'));
    $changed = array(); $count = 0;
    foreach ($q->posts as $pid) {
        $post = get_post($pid);
        $hits = substr_count($post->post_content, $find) + ($also_title ? substr_count($post->post_title, $find) : 0);
        if ($hits < 1) continue;
        $count += $hits;
        $changed[] = array('post_id' => $pid, 'title' => get_the_title($pid), 'occurrences' => $hits);
        if (!$dry) {
            $upd = array('ID' => $pid, 'post_content' => str_replace($find, $replace, $post->post_content));
            if ($also_title) $upd['post_title'] = str_replace($find, $replace, $post->post_title);
            wp_update_post($upd);
        }
    }
    if (!$dry) falcon_seo_log(0, 'find_replace', array('find' => $find, 'replace' => $replace, 'posts' => count($changed), 'occurrences' => $count), falcon_seo_reason($b));
    return rest_ensure_response(array('dry_run' => $dry, 'posts_affected' => count($changed), 'total_occurrences' => $count, 'details' => $changed));
}
function falcon_seo_rest_schedule_post(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $when = isset($b['publish_at']) ? sanitize_text_field($b['publish_at']) : '';
    if ($when === '') return new WP_Error('falcon_empty', 'publish_at (ISO datetime) is required.', array('status' => 400));
    $ts = strtotime($when);
    if (!$ts) return new WP_Error('falcon_err', 'Could not parse publish_at.', array('status' => 400));
    $gmt = gmdate('Y-m-d H:i:s', $ts);
    $local = get_date_from_gmt($gmt);
    $pid = (int) ($b['post_id'] ?? 0);
    if ($pid) {
        if (!get_post($pid)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
        wp_update_post(array('ID' => $pid, 'post_status' => 'future', 'post_date' => $local, 'post_date_gmt' => $gmt));
    } else {
        $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
        if ($title === '') return new WP_Error('falcon_empty', 'title is required to create a scheduled post.', array('status' => 400));
        $pid = wp_insert_post(array('post_title' => $title, 'post_content' => isset($b['content']) ? wp_kses_post($b['content']) : '',
            'post_type' => sanitize_key($b['type'] ?? 'post'), 'post_status' => 'future', 'post_date' => $local, 'post_date_gmt' => $gmt,
            'post_author' => falcon_seo_default_author()), true);
        if (is_wp_error($pid)) return new WP_Error('falcon_err', $pid->get_error_message(), array('status' => 400));
    }
    falcon_seo_log($pid, 'schedule_post', array('publish_at' => $gmt), falcon_seo_reason($b));
    return rest_ensure_response(array('scheduled' => true, 'post_id' => $pid, 'publish_at_gmt' => $gmt, 'status' => get_post_status($pid), 'url' => get_permalink($pid)));
}
function falcon_seo_rest_content_calendar(WP_REST_Request $req) {
    $q = new WP_Query(array('post_type' => array('post', 'page'), 'post_status' => 'future', 'posts_per_page' => 100, 'orderby' => 'date', 'order' => 'ASC'));
    $rows = array();
    foreach ($q->posts as $p) $rows[] = array('id' => $p->ID, 'title' => get_the_title($p), 'type' => $p->post_type, 'publish_at' => $p->post_date_gmt);
    return rest_ensure_response(array('count' => count($rows), 'scheduled' => $rows));
}
function falcon_seo_rest_missing_alt(WP_REST_Request $req) {
    $per = $req->get_param('per_page') ? min(300, max(1, (int) $req->get_param('per_page'))) : 100;
    $q = new WP_Query(array('post_type' => 'attachment', 'post_mime_type' => 'image', 'post_status' => 'inherit', 'posts_per_page' => $per));
    $rows = array();
    foreach ($q->posts as $p) {
        if (get_post_meta($p->ID, '_wp_attachment_image_alt', true) === '') {
            $rows[] = array('id' => $p->ID, 'title' => get_the_title($p), 'url' => wp_get_attachment_url($p->ID), 'filename' => basename((string) get_attached_file($p->ID)));
        }
    }
    return rest_ensure_response(array('count' => count($rows), 'images_missing_alt' => $rows));
}
function falcon_seo_rest_set_alt(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    if (!empty($b['items']) && is_array($b['items'])) {
        $res = array();
        foreach ($b['items'] as $it) {
            $id = (int) ($it['media_id'] ?? $it['id'] ?? 0);
            if (!$id) continue;
            update_post_meta($id, '_wp_attachment_image_alt', sanitize_text_field($it['alt'] ?? ''));
            $res[] = $id;
        }
        falcon_seo_log(0, 'set_image_alt', array('count' => count($res)), falcon_seo_reason($b));
        return rest_ensure_response(array('updated' => $res, 'count' => count($res)));
    }
    $id = (int) ($b['media_id'] ?? 0);
    if (!$id) return new WP_Error('falcon_empty', 'media_id (or items[]) with alt is required.', array('status' => 400));
    update_post_meta($id, '_wp_attachment_image_alt', sanitize_text_field($b['alt'] ?? ''));
    falcon_seo_log($id, 'set_image_alt', array('media_id' => $id), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'media_id' => $id, 'alt' => get_post_meta($id, '_wp_attachment_image_alt', true)));
}
function falcon_seo_rest_list_redirects() {
    $r = get_option('falcon_seo_redirects', array());
    return rest_ensure_response(array('count' => count($r), 'redirects' => array_values($r)));
}
function falcon_seo_rest_add_redirect(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $from = isset($b['from']) ? sanitize_text_field($b['from']) : '';
    $to = isset($b['to']) ? esc_url_raw($b['to']) : '';
    if (!$from || !$to) return new WP_Error('falcon_empty', 'from and to are required.', array('status' => 400));
    $type = (int) ($b['type'] ?? 301);
    $redirects = get_option('falcon_seo_redirects', array());
    $id = 'r' . (count($redirects) + 1) . '_' . substr(md5($from . microtime(true)), 0, 6);
    $redirects[$id] = array('id' => $id, 'from' => $from, 'to' => $to, 'type' => in_array($type, array(301, 302, 307), true) ? $type : 301);
    update_option('falcon_seo_redirects', $redirects);
    falcon_seo_log(0, 'add_redirect', array('from' => $from, 'to' => $to, 'type' => $type), falcon_seo_reason($b));
    return rest_ensure_response(array('added' => true, 'redirect' => $redirects[$id]));
}
function falcon_seo_rest_delete_redirect(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $id = isset($b['id']) ? sanitize_text_field($b['id']) : '';
    $redirects = get_option('falcon_seo_redirects', array());
    if (!isset($redirects[$id])) return new WP_Error('falcon_not_found', 'Redirect not found.', array('status' => 404));
    unset($redirects[$id]);
    update_option('falcon_seo_redirects', $redirects);
    falcon_seo_log(0, 'delete_redirect', array('id' => $id), '');
    return rest_ensure_response(array('deleted' => true, 'id' => $id));
}
function falcon_seo_rest_get_robots() {
    $custom = get_option('falcon_seo_robots', '');
    return rest_ensure_response(array('custom' => $custom, 'using_custom' => $custom !== '', 'robots_url' => home_url('/robots.txt')));
}
function falcon_seo_rest_update_robots(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    if (!isset($b['content'])) return new WP_Error('falcon_empty', 'content is required (empty string resets to the WordPress default).', array('status' => 400));
    $content = (string) $b['content'];
    if ($content === '') delete_option('falcon_seo_robots'); else update_option('falcon_seo_robots', $content);
    falcon_seo_log(0, 'update_robots', array('bytes' => strlen($content)), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'using_custom' => $content !== '', 'robots_url' => home_url('/robots.txt')));
}
function falcon_seo_rest_get_sitemaps() {
    $out = array('core_sitemap' => null, 'yoast_sitemap' => null, 'blog_public' => (bool) get_option('blog_public'));
    if (function_exists('wp_sitemaps_get_server') && get_option('blog_public')) $out['core_sitemap'] = home_url('/wp-sitemap.xml');
    if (falcon_seo_yoast_active()) $out['yoast_sitemap'] = home_url('/sitemap_index.xml');
    return rest_ensure_response($out);
}
function falcon_seo_rest_get_schema(WP_REST_Request $req) {
    $id = (int) $req['id'];
    return rest_ensure_response(array('post_id' => $id, 'schema' => get_post_meta($id, '_falcon_schema_jsonld', true)));
}
function falcon_seo_rest_set_schema(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $schema = $b['schema'] ?? null;
    if ($schema === null) return new WP_Error('falcon_empty', 'schema is required (object or JSON string; empty string removes it).', array('status' => 400));
    if (is_array($schema)) $schema = wp_json_encode($schema);
    $schema = (string) $schema;
    if (trim($schema) === '') {
        delete_post_meta($id, '_falcon_schema_jsonld');
    } else {
        json_decode($schema);
        if (json_last_error() !== JSON_ERROR_NONE) return new WP_Error('falcon_err', 'schema is not valid JSON.', array('status' => 400));
        update_post_meta($id, '_falcon_schema_jsonld', wp_slash($schema));
    }
    falcon_seo_log($id, 'set_schema', array('bytes' => strlen($schema)), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $id));
}
function falcon_seo_rest_set_social(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $changed = array();
    if (falcon_seo_yoast_active()) {
        $map = array('og_title' => '_yoast_wpseo_opengraph-title', 'og_description' => '_yoast_wpseo_opengraph-description',
            'twitter_title' => '_yoast_wpseo_twitter-title', 'twitter_description' => '_yoast_wpseo_twitter-description');
        foreach ($map as $k => $meta) if (isset($b[$k])) { update_post_meta($id, $meta, sanitize_text_field($b[$k])); $changed[$k] = $b[$k]; }
        if (!empty($b['og_image_url'])) { update_post_meta($id, '_yoast_wpseo_opengraph-image', esc_url_raw($b['og_image_url'])); $changed['og_image'] = $b['og_image_url']; }
        if (!empty($b['twitter_image_url'])) { update_post_meta($id, '_yoast_wpseo_twitter-image', esc_url_raw($b['twitter_image_url'])); $changed['twitter_image'] = $b['twitter_image_url']; }
    } else {
        $social = (array) get_post_meta($id, '_falcon_social', true);
        foreach (array('og_title', 'og_description', 'twitter_title', 'twitter_description') as $k) if (isset($b[$k])) { $social[$k] = sanitize_text_field($b[$k]); $changed[$k] = $b[$k]; }
        if (!empty($b['og_image_url'])) { $social['og_image'] = esc_url_raw($b['og_image_url']); $changed['og_image'] = $b['og_image_url']; }
        if (!empty($b['twitter_image_url'])) { $social['twitter_image'] = esc_url_raw($b['twitter_image_url']); $changed['twitter_image'] = $b['twitter_image_url']; }
        update_post_meta($id, '_falcon_social', $social);
    }
    falcon_seo_log($id, 'set_social_meta', $changed, falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $id, 'via' => falcon_seo_yoast_active() ? 'yoast' : 'falcon', 'changed' => $changed));
}

/* ============================================================
 * Structure & builders
 * ============================================================ */
function falcon_seo_blocks_to_html($blocks) {
    $html = '';
    foreach ($blocks as $blk) {
        $type = $blk['type'] ?? 'paragraph';
        $text = $blk['text'] ?? ($blk['content'] ?? '');
        switch ($type) {
            case 'heading':
                $lvl = max(1, min(6, (int) ($blk['level'] ?? 2)));
                $html .= "<!-- wp:heading {\"level\":{$lvl}} -->\n<h{$lvl}>" . esc_html($text) . "</h{$lvl}>\n<!-- /wp:heading -->\n\n";
                break;
            case 'image':
                $url = esc_url($blk['url'] ?? '');
                $html .= "<!-- wp:image -->\n<figure class=\"wp-block-image\"><img src=\"{$url}\" alt=\"" . esc_attr($blk['alt'] ?? '') . "\"/></figure>\n<!-- /wp:image -->\n\n";
                break;
            case 'list':
                $li = '';
                foreach (($blk['items'] ?? array()) as $i) $li .= '<li>' . esc_html($i) . '</li>';
                $html .= "<!-- wp:list -->\n<ul>{$li}</ul>\n<!-- /wp:list -->\n\n";
                break;
            case 'button':
                $url = esc_url($blk['url'] ?? '#');
                $html .= "<!-- wp:buttons -->\n<div class=\"wp-block-buttons\"><!-- wp:button -->\n<div class=\"wp-block-button\"><a class=\"wp-block-button__link wp-element-button\" href=\"{$url}\">" . esc_html($text) . "</a></div>\n<!-- /wp:button --></div>\n<!-- /wp:buttons -->\n\n";
                break;
            case 'quote':
                $html .= "<!-- wp:quote -->\n<blockquote class=\"wp-block-quote\"><p>" . esc_html($text) . "</p></blockquote>\n<!-- /wp:quote -->\n\n";
                break;
            case 'html':
                $html .= "<!-- wp:html -->\n" . wp_kses_post($blk['html'] ?? $text) . "\n<!-- /wp:html -->\n\n";
                break;
            default:
                $html .= "<!-- wp:paragraph -->\n<p>" . wp_kses_post($text) . "</p>\n<!-- /wp:paragraph -->\n\n";
        }
    }
    return $html;
}
function falcon_seo_rest_build_gutenberg(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $blocks = $b['blocks'] ?? null;
    if (!is_array($blocks) || !$blocks) return new WP_Error('falcon_empty', 'blocks (array) is required.', array('status' => 400));
    $content = falcon_seo_blocks_to_html($blocks);
    $pid = (int) ($b['post_id'] ?? 0);
    if ($pid) {
        if (!get_post($pid)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
        $new = !empty($b['append']) ? (get_post($pid)->post_content . "\n\n" . $content) : $content;
        wp_update_post(array('ID' => $pid, 'post_content' => $new));
    } else {
        $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
        if ($title === '') return new WP_Error('falcon_empty', 'title is required to create a page.', array('status' => 400));
        $pid = wp_insert_post(array('post_title' => $title, 'post_content' => $content, 'post_type' => sanitize_key($b['type'] ?? 'page'),
            'post_status' => in_array(($b['status'] ?? 'draft'), array('draft', 'publish', 'pending', 'private'), true) ? $b['status'] : 'draft',
            'post_author' => falcon_seo_default_author()), true);
        if (is_wp_error($pid)) return new WP_Error('falcon_err', $pid->get_error_message(), array('status' => 400));
    }
    falcon_seo_log($pid, 'build_gutenberg', array('blocks' => count($blocks)), falcon_seo_reason($b));
    return rest_ensure_response(array('saved' => true, 'post_id' => $pid, 'url' => get_permalink($pid), 'edit_url' => admin_url("post.php?post={$pid}&action=edit")));
}
function falcon_seo_rest_get_elementor(WP_REST_Request $req) {
    if (!defined('ELEMENTOR_VERSION')) return new WP_Error('falcon_err', 'Elementor is not active on this site.', array('status' => 400));
    $id = (int) $req['id'];
    return rest_ensure_response(array('post_id' => $id, 'edit_mode' => get_post_meta($id, '_elementor_edit_mode', true), 'elementor_data' => get_post_meta($id, '_elementor_data', true)));
}
function falcon_seo_rest_set_elementor(WP_REST_Request $req) {
    if (!defined('ELEMENTOR_VERSION')) return new WP_Error('falcon_err', 'Elementor is not active on this site.', array('status' => 400));
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $data = $b['elementor_data'] ?? null;
    if ($data === null) return new WP_Error('falcon_empty', 'elementor_data (JSON string or array) is required.', array('status' => 400));
    if (is_array($data)) $data = wp_json_encode($data);
    json_decode($data);
    if (json_last_error() !== JSON_ERROR_NONE) return new WP_Error('falcon_err', 'elementor_data is not valid JSON.', array('status' => 400));
    update_post_meta($id, '_elementor_data', wp_slash($data));
    update_post_meta($id, '_elementor_edit_mode', 'builder');
    if (class_exists('\Elementor\Plugin')) {
        try { \Elementor\Plugin::$instance->files_manager->clear_cache(); } catch (\Throwable $e) {}
    }
    falcon_seo_log($id, 'set_elementor', array('bytes' => strlen($data)), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $id));
}

// Unique 8-char Elementor element id (uniqid() alone collides inside tight loops).
function falcon_seo_eid(&$c) { $c++; return substr(md5('falcon_el_' . $c . '_' . microtime()), 0, 8); }

// Build a single Elementor widget from one block (free widgets only). Returns null for invalid input.
function falcon_seo_block_to_widget(&$c, $blk) {
    if (!is_array($blk)) return null;
    $type = isset($blk['type']) ? strtolower($blk['type']) : 'paragraph';
    $text = isset($blk['text']) ? $blk['text'] : '';
    switch ($type) {
        case 'heading':
            $widget = array('widgetType' => 'heading', 'settings' => array(
                'title' => wp_kses_post($text),
                'header_size' => isset($blk['level']) ? sanitize_key($blk['level']) : 'h2',
                'align' => isset($blk['align']) ? sanitize_key($blk['align']) : 'left'));
            break;
        case 'button':
            $widget = array('widgetType' => 'button', 'settings' => array(
                'text' => sanitize_text_field($text !== '' ? $text : 'Click here'),
                'link' => array('url' => isset($blk['link']) ? esc_url_raw($blk['link']) : '', 'is_external' => '', 'nofollow' => ''),
                'align' => isset($blk['align']) ? sanitize_key($blk['align']) : 'left'));
            break;
        case 'image':
            $widget = array('widgetType' => 'image', 'settings' => array(
                'image' => array('url' => isset($blk['url']) ? esc_url_raw($blk['url']) : ''),
                'align' => isset($blk['align']) ? sanitize_key($blk['align']) : 'center'));
            break;
        case 'spacer':
            $widget = array('widgetType' => 'spacer', 'settings' => array(
                'space' => array('unit' => 'px', 'size' => isset($blk['size']) ? (int) $blk['size'] : 50)));
            break;
        case 'divider':
            $widget = array('widgetType' => 'divider', 'settings' => array());
            break;
        case 'video':
            $widget = array('widgetType' => 'video', 'settings' => array(
                'youtube_url' => isset($blk['url']) ? esc_url_raw($blk['url']) : ''));
            break;
        case 'html':
            $widget = array('widgetType' => 'html', 'settings' => array(
                'html' => isset($blk['html']) ? $blk['html'] : $text));
            break;
        default: // paragraph / text / anything else
            $widget = array('widgetType' => 'text-editor', 'settings' => array('editor' => wp_kses_post(wpautop($text))));
    }
    $widget['id'] = falcon_seo_eid($c);
    $widget['elType'] = 'widget';
    $widget['elements'] = array();
    return $widget;
}

// Build one Elementor column ($size = %, $inline = custom width % or null) from a list of blocks.
function falcon_seo_make_column(&$c, $blocks, $size, $inline) {
    $widgets = array();
    foreach ((array) $blocks as $b) {
        $w = falcon_seo_block_to_widget($c, $b);
        if ($w) $widgets[] = $w;
    }
    return array('id' => falcon_seo_eid($c), 'elType' => 'column',
        'settings' => array('_column_size' => $size, '_inline_size' => $inline),
        'elements' => $widgets);
}

// Section-level background (hex) + padding (px) shared by single and multi-column blocks.
function falcon_seo_section_settings($blk) {
    $s = array();
    if (!empty($blk['background'])) {
        $s['background_background'] = 'classic';
        $hex = sanitize_hex_color($blk['background']);
        $s['background_color'] = $hex ? $hex : sanitize_text_field($blk['background']);
    }
    if (isset($blk['padding'])) {
        $p = (int) $blk['padding'];
        $s['padding'] = array('unit' => 'px', 'top' => $p, 'right' => $p, 'bottom' => $p, 'left' => $p, 'isLinked' => true);
    }
    return $s;
}

// Turn a simple block list into Elementor's nested section/column/widget JSON (free widgets only).
// A block is either a single widget (heading/paragraph/button/...) → one full-width column, OR a
// {type:"columns"|"row", columns:[...]} container → a section with N side-by-side columns. Each entry
// in `columns` is a list of blocks, or {blocks:[...], width?:percent} for a custom column width.
function falcon_seo_blocks_to_elementor($blocks) {
    $c = 0; $sections = array();
    foreach ($blocks as $blk) {
        if (!is_array($blk)) continue;
        $type = isset($blk['type']) ? strtolower($blk['type']) : '';
        if ($type === 'columns' || $type === 'row') {
            $cols_in = (isset($blk['columns']) && is_array($blk['columns'])) ? $blk['columns'] : array();
            if (!$cols_in) continue;
            $n = count($cols_in);
            $has_custom = false;
            foreach ($cols_in as $col) { if (is_array($col) && isset($col['width'])) { $has_custom = true; break; } }
            $columns = array();
            foreach ($cols_in as $col) {
                if (is_array($col) && isset($col['blocks'])) { $cblocks = $col['blocks']; $w = isset($col['width']) ? (float) $col['width'] : null; }
                else { $cblocks = $col; $w = null; }              // a plain list of blocks
                if ($has_custom) {
                    $size = $w !== null ? (int) round($w) : (int) round(100 / $n);
                    $inline = $w !== null ? $w : null;
                } else {
                    $size = (int) round(100 / $n);
                    $inline = null;
                }
                $columns[] = falcon_seo_make_column($c, $cblocks, $size, $inline);
            }
            $sections[] = array('id' => falcon_seo_eid($c), 'elType' => 'section', 'settings' => falcon_seo_section_settings($blk), 'elements' => $columns);
        } else {
            $column = falcon_seo_make_column($c, array($blk), 100, null);   // single widget → one full-width column
            if (empty($column['elements'])) continue;
            $sections[] = array('id' => falcon_seo_eid($c), 'elType' => 'section', 'settings' => falcon_seo_section_settings($blk), 'elements' => array($column));
        }
    }
    return $sections;
}

// Build an Elementor page from a simple block list (free widgets). Creates a page or rebuilds one.
function falcon_seo_rest_build_elementor(WP_REST_Request $req) {
    if (!defined('ELEMENTOR_VERSION')) return new WP_Error('falcon_err', 'Elementor is not active on this site.', array('status' => 400));
    $b = falcon_seo_body($req);
    $blocks = (isset($b['blocks']) && is_array($b['blocks'])) ? $b['blocks'] : null;
    if (!$blocks) return new WP_Error('falcon_empty', 'blocks (array) is required.', array('status' => 400));
    $pid = (int) ($b['post_id'] ?? 0);
    if ($pid) {
        if (!get_post($pid)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    } else {
        $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
        if ($title === '') return new WP_Error('falcon_empty', 'title is required to create a page.', array('status' => 400));
        $pid = wp_insert_post(array('post_title' => $title, 'post_type' => sanitize_key($b['type'] ?? 'page'),
            'post_status' => in_array(($b['status'] ?? 'draft'), array('draft', 'publish', 'pending', 'private'), true) ? $b['status'] : 'draft',
            'post_author' => falcon_seo_default_author()), true);
        if (is_wp_error($pid)) return new WP_Error('falcon_err', $pid->get_error_message(), array('status' => 400));
    }
    $data = falcon_seo_blocks_to_elementor($blocks);
    update_post_meta($pid, '_elementor_data', wp_slash(wp_json_encode($data)));
    update_post_meta($pid, '_elementor_edit_mode', 'builder');
    update_post_meta($pid, '_elementor_version', ELEMENTOR_VERSION);
    update_post_meta($pid, '_elementor_template_type', 'wp-page');
    if (!empty($b['canvas'])) update_post_meta($pid, '_wp_page_template', 'elementor_canvas');
    if (class_exists('\Elementor\Plugin')) { try { \Elementor\Plugin::$instance->files_manager->clear_cache(); } catch (\Throwable $e) {} }
    falcon_seo_log($pid, 'build_elementor', array('blocks' => count($blocks)), falcon_seo_reason($b));
    return rest_ensure_response(array('saved' => true, 'post_id' => $pid, 'sections' => count($data),
        'url' => get_permalink($pid), 'edit_url' => admin_url("post.php?post={$pid}&action=elementor")));
}
function falcon_seo_rest_get_fields(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $out = array('post_id' => $id, 'acf_active' => function_exists('get_fields'), 'fields' => array(), 'meta' => array());
    if (function_exists('get_fields')) { $f = get_fields($id); $out['fields'] = $f ? $f : array(); }
    foreach (get_post_meta($id) as $k => $v) {
        if (strpos($k, '_') === 0) continue;
        $out['meta'][$k] = maybe_unserialize(is_array($v) ? ($v[0] ?? '') : $v);
    }
    return rest_ensure_response($out);
}
function falcon_seo_rest_set_field(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $key = isset($b['key']) ? sanitize_text_field($b['key']) : '';
    if ($key === '') return new WP_Error('falcon_empty', 'key is required.', array('status' => 400));
    $value = $b['value'] ?? '';
    if (function_exists('update_field') && !empty($b['acf'])) update_field($key, $value, $id);
    else update_post_meta($id, $key, $value);
    falcon_seo_log($id, 'set_custom_field', array('key' => $key), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $id, 'key' => $key));
}

// List ACF field groups + their fields (definitions, not values).
function falcon_seo_rest_acf_list_groups() {
    if (!function_exists('acf_get_field_groups')) {
        return new WP_Error('falcon_err', 'ACF is not active on this site (need Advanced Custom Fields).', array('status' => 400));
    }
    $out = array();
    foreach (acf_get_field_groups() as $g) {
        $frows = array();
        $fields = function_exists('acf_get_fields') ? acf_get_fields($g) : array();
        if (is_array($fields)) {
            foreach ($fields as $f) {
                $frows[] = array(
                    'key'   => isset($f['key']) ? $f['key'] : '',
                    'name'  => isset($f['name']) ? $f['name'] : '',
                    'label' => isset($f['label']) ? $f['label'] : '',
                    'type'  => isset($f['type']) ? $f['type'] : '',
                );
            }
        }
        $out[] = array(
            'key'      => isset($g['key']) ? $g['key'] : '',
            'id'       => isset($g['ID']) ? (int) $g['ID'] : 0,
            'title'    => isset($g['title']) ? $g['title'] : '',
            'active'   => isset($g['active']) ? (bool) $g['active'] : true,
            'location' => isset($g['location']) ? $g['location'] : array(),
            'fields'   => $frows,
        );
    }
    return rest_ensure_response(array('acf_active' => true, 'count' => count($out), 'field_groups' => $out));
}

// Create an ACF field group (definitions) in the DB via ACF's own importer — visible/editable in WP-admin.
function falcon_seo_rest_acf_create_group(WP_REST_Request $req) {
    if (!function_exists('acf_import_field_group')) {
        return new WP_Error('falcon_err', 'ACF is not active on this site (need Advanced Custom Fields).', array('status' => 400));
    }
    $b = falcon_seo_body($req);
    $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
    if ($title === '') return new WP_Error('falcon_empty', 'title is required.', array('status' => 400));
    $in_fields = (isset($b['fields']) && is_array($b['fields'])) ? $b['fields'] : array();
    if (!$in_fields) return new WP_Error('falcon_empty', 'fields (array) is required.', array('status' => 400));

    // Location: explicit raw `location` wins; else build OR-of post_type rules from post_types[]; else default to post.
    if (!empty($b['location']) && is_array($b['location'])) {
        $location = $b['location'];
    } elseif (!empty($b['post_types']) && is_array($b['post_types'])) {
        $location = array();
        foreach ($b['post_types'] as $pt) {
            $location[] = array(array('param' => 'post_type', 'operator' => '==', 'value' => sanitize_key($pt)));
        }
    } else {
        $location = array(array(array('param' => 'post_type', 'operator' => '==', 'value' => 'post')));
    }

    $group_key = !empty($b['key']) ? sanitize_key($b['key']) : uniqid('group_');
    $fields = array();
    $i = 0;
    foreach ($in_fields as $f) {
        if (!is_array($f)) continue;
        $label = isset($f['label']) ? sanitize_text_field($f['label']) : (isset($f['name']) ? sanitize_text_field($f['name']) : '');
        if ($label === '' && empty($f['name'])) continue;
        $name = (isset($f['name']) && $f['name'] !== '') ? sanitize_key($f['name']) : str_replace('-', '_', sanitize_title($label));
        // Deterministic, collision-free key (uniqid() in a tight loop can repeat).
        $key  = (isset($f['key']) && $f['key'] !== '') ? sanitize_key($f['key']) : 'field_' . substr(md5($group_key . '_' . $name . '_' . $i), 0, 13);
        $field = array('key' => $key, 'label' => ($label !== '' ? $label : $name), 'name' => $name,
                       'type' => (isset($f['type']) && $f['type'] !== '') ? sanitize_key($f['type']) : 'text');
        foreach (array('instructions', 'default_value', 'placeholder', 'choices', 'min', 'max', 'step', 'return_format', 'ui', 'multiple', 'allow_null') as $opt) {
            if (isset($f[$opt])) $field[$opt] = $f[$opt];
        }
        if (isset($f['required'])) $field['required'] = $f['required'] ? 1 : 0;
        $fields[] = $field;
        $i++;
    }
    if (!$fields) return new WP_Error('falcon_empty', 'no valid fields provided.', array('status' => 400));

    $group = array(
        'key' => $group_key, 'title' => $title, 'fields' => $fields, 'location' => $location,
        'active' => isset($b['active']) ? (bool) $b['active'] : true,
        'menu_order' => 0, 'position' => 'normal', 'style' => 'default', 'label_placement' => 'top',
    );
    $result = acf_import_field_group($group);
    $gid = (is_array($result) && isset($result['ID'])) ? (int) $result['ID'] : 0;
    falcon_seo_log($gid, 'create_acf_field_group', array('title' => $title, 'fields' => count($fields)), falcon_seo_reason($b));
    return rest_ensure_response(array(
        'created'     => true,
        'group_id'    => $gid,
        'group_key'   => (is_array($result) && isset($result['key'])) ? $result['key'] : $group_key,
        'title'       => $title,
        'field_count' => count($fields),
        'field_names' => array_map(function ($f) { return $f['name']; }, $fields),
        'edit_url'    => $gid ? admin_url("post.php?post={$gid}&action=edit") : '',
    ));
}

function falcon_seo_rest_widget_areas() {
    global $wp_registered_sidebars;
    $sidebars = get_option('sidebars_widgets', array());
    $rows = array();
    foreach ((array) $wp_registered_sidebars as $id => $s) {
        $rows[] = array('id' => $id, 'name' => $s['name'] ?? $id, 'widgets' => isset($sidebars[$id]) ? array_values($sidebars[$id]) : array());
    }
    return rest_ensure_response(array('count' => count($rows), 'widget_areas' => $rows,
        'note' => 'On block-widget themes (WP 5.8+), classic widgets may not render unless the Classic Widgets plugin is active.'));
}
function falcon_seo_rest_add_widget(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $sidebar = isset($b['sidebar_id']) ? sanitize_text_field($b['sidebar_id']) : '';
    $content = isset($b['content']) ? wp_kses_post($b['content']) : '';
    if (!$sidebar || $content === '') return new WP_Error('falcon_empty', 'sidebar_id and content are required.', array('status' => 400));
    $widgets = get_option('widget_custom_html', array());
    $next = 1;
    foreach (array_keys($widgets) as $k) if (is_numeric($k) && $k >= $next) $next = $k + 1;
    $widgets[$next] = array('title' => isset($b['title']) ? sanitize_text_field($b['title']) : '', 'content' => $content);
    $widgets['_multiwidget'] = 1;
    update_option('widget_custom_html', $widgets);
    $sidebars = get_option('sidebars_widgets', array());
    if (!isset($sidebars[$sidebar]) || !is_array($sidebars[$sidebar])) $sidebars[$sidebar] = array();
    $sidebars[$sidebar][] = 'custom_html-' . $next;
    update_option('sidebars_widgets', $sidebars);
    falcon_seo_log(0, 'add_widget', array('sidebar' => $sidebar), falcon_seo_reason($b));
    return rest_ensure_response(array('added' => true, 'sidebar_id' => $sidebar, 'widget' => 'custom_html-' . $next));
}
function falcon_seo_rest_post_types() {
    $rows = array();
    foreach (get_post_types(array('public' => true), 'objects') as $pt) {
        $counts = wp_count_posts($pt->name);
        $rows[] = array('slug' => $pt->name, 'label' => $pt->label, 'rest_base' => $pt->rest_base ? $pt->rest_base : $pt->name,
            'published' => isset($counts->publish) ? (int) $counts->publish : 0, 'builtin' => (bool) $pt->_builtin);
    }
    return rest_ensure_response(array('count' => count($rows), 'post_types' => $rows));
}

/* ============================================================
 * Maintenance & safety
 * ============================================================ */
// The folder name carries a per-site random secret so backup/export URLs aren't
// guessable even on servers (e.g. Nginx) that don't honor the .htaccess deny rule below.
function falcon_seo_backup_dir_slug() {
    $slug = get_option('falcon_seo_backup_slug');
    if (!$slug) {
        $slug = 'falcon-backups-' . substr(wp_generate_password(20, false, false), 0, 12);
        update_option('falcon_seo_backup_slug', $slug);
    }
    return $slug;
}
function falcon_seo_backup_dir() {
    $up = wp_get_upload_dir();
    $slug = falcon_seo_backup_dir_slug();
    $dir = $up['basedir'] . '/' . $slug;
    if (!is_dir($dir)) {
        wp_mkdir_p($dir);
        @file_put_contents($dir . '/.htaccess', "Require all denied\n");
        @file_put_contents($dir . '/index.php', "<?php // Silence is golden.\n");
    }
    return array($dir, $up['baseurl'] . '/' . $slug);
}
function falcon_seo_quick_backup($note = '') {
    list($dir, $url) = falcon_seo_backup_dir();
    $q = new WP_Query(array('post_type' => array('post', 'page'), 'post_status' => 'any', 'posts_per_page' => -1));
    $data = array();
    foreach ($q->posts as $p) {
        $data[] = array('ID' => $p->ID, 'title' => $p->post_title, 'content' => $p->post_content,
            'excerpt' => $p->post_excerpt, 'status' => $p->post_status, 'type' => $p->post_type);
    }
    $name = 'content-' . gmdate('Ymd-His') . '.json';
    file_put_contents($dir . '/' . $name, wp_json_encode(array('note' => $note, 'created' => gmdate('c'), 'posts' => $data)));
    return array('file' => $name, 'url' => $url . '/' . $name, 'posts' => count($data));
}
function falcon_seo_rest_create_backup(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $r = falcon_seo_quick_backup(isset($b['note']) ? sanitize_text_field($b['note']) : 'manual');
    falcon_seo_log(0, 'create_backup', $r, falcon_seo_reason($b));
    return rest_ensure_response(array_merge(array('created' => true), $r,
        array('note' => 'Backs up post/page content as JSON (download-protected). Not a full DB/file backup — use a host backup for that.')));
}
function falcon_seo_rest_list_backups() {
    list($dir, $url) = falcon_seo_backup_dir();
    $rows = array();
    foreach (glob($dir . '/*.json') as $f) {
        $rows[] = array('file' => basename($f), 'url' => $url . '/' . basename($f), 'size' => filesize($f), 'modified' => gmdate('c', filemtime($f)));
    }
    usort($rows, function ($a, $b) { return strcmp($b['file'], $a['file']); });
    return rest_ensure_response(array('count' => count($rows), 'backups' => $rows));
}
function falcon_seo_rest_db_status() {
    global $wpdb;
    return rest_ensure_response(array(
        'revisions' => (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->posts} WHERE post_type='revision'"),
        'auto_drafts' => (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->posts} WHERE post_status='auto-draft'"),
        'trashed_posts' => (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->posts} WHERE post_status='trash'"),
        'spam_comments' => (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->comments} WHERE comment_approved='spam'"),
        'trashed_comments' => (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->comments} WHERE comment_approved='trash'"),
        'transients' => (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->options} WHERE option_name LIKE '\\_transient\\_%' OR option_name LIKE '\\_site\\_transient\\_%'"),
    ));
}
function falcon_seo_rest_db_cleanup(WP_REST_Request $req) {
    global $wpdb;
    $b = falcon_seo_body($req);
    $all = !empty($b['all']);
    $done = array();
    if ($all || !empty($b['revisions']))   $done['revisions'] = (int) $wpdb->query("DELETE FROM {$wpdb->posts} WHERE post_type='revision'");
    if ($all || !empty($b['auto_drafts'])) $done['auto_drafts'] = (int) $wpdb->query("DELETE FROM {$wpdb->posts} WHERE post_status='auto-draft'");
    if ($all || !empty($b['spam']))        $done['spam_comments'] = (int) $wpdb->query("DELETE FROM {$wpdb->comments} WHERE comment_approved='spam'");
    if ($all || !empty($b['trash']))       $done['trashed_comments'] = (int) $wpdb->query("DELETE FROM {$wpdb->comments} WHERE comment_approved='trash'");
    if ($all || !empty($b['transients']))  $done['transients'] = (int) $wpdb->query("DELETE FROM {$wpdb->options} WHERE option_name LIKE '\\_transient\\_%' OR option_name LIKE '\\_site\\_transient\\_%'");
    if ($all || !empty($b['orphan_meta'])) $done['orphan_postmeta'] = (int) $wpdb->query("DELETE pm FROM {$wpdb->postmeta} pm LEFT JOIN {$wpdb->posts} p ON p.ID = pm.post_id WHERE p.ID IS NULL");
    falcon_seo_log(0, 'db_cleanup', $done, falcon_seo_reason($b));
    return rest_ensure_response(array('cleaned' => true, 'deleted' => $done));
}
function falcon_seo_rest_clear_cache(WP_REST_Request $req) {
    $cleared = array();
    if (function_exists('rocket_clean_domain')) { rocket_clean_domain(); $cleared[] = 'WP Rocket'; }
    if (defined('LSCWP_V')) { do_action('litespeed_purge_all'); $cleared[] = 'LiteSpeed Cache'; }
    if (function_exists('w3tc_flush_all')) { w3tc_flush_all(); $cleared[] = 'W3 Total Cache'; }
    if (function_exists('wp_cache_clear_cache')) { wp_cache_clear_cache(); $cleared[] = 'WP Super Cache'; }
    if (function_exists('sg_cachepress_purge_cache')) { sg_cachepress_purge_cache(); $cleared[] = 'SiteGround'; }
    if (has_action('cache_enabler_clear_complete_cache')) { do_action('cache_enabler_clear_complete_cache'); $cleared[] = 'Cache Enabler'; }
    wp_cache_flush();
    $cleared[] = 'Object cache';
    falcon_seo_log(0, 'clear_cache', array('cleared' => $cleared), '');
    return rest_ensure_response(array('cleared' => $cleared));
}
function falcon_seo_rest_performance() {
    global $wpdb;
    $active = count((array) get_option('active_plugins', array()));
    $autoload_size = (int) $wpdb->get_var("SELECT SUM(LENGTH(option_value)) FROM {$wpdb->options} WHERE autoload='yes'");
    $autoload_count = (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->options} WHERE autoload='yes'");
    $big = $wpdb->get_results("SELECT option_name, LENGTH(option_value) AS sz FROM {$wpdb->options} WHERE autoload='yes' ORDER BY sz DESC LIMIT 5", ARRAY_A);
    $issues = array();
    if ($autoload_size > 1000000) $issues[] = 'Autoloaded options are large (' . round($autoload_size / 1024) . ' KB) — slows every page load.';
    if ($active > 30) $issues[] = $active . ' active plugins — consider trimming.';
    if (version_compare(PHP_VERSION, '8.0', '<')) $issues[] = 'PHP ' . PHP_VERSION . ' is outdated — upgrade to 8.1+ for speed.';
    if (!wp_using_ext_object_cache()) $issues[] = 'No persistent object cache (Redis/Memcached) — add one for a busy site.';
    return rest_ensure_response(array(
        'active_plugins' => $active,
        'autoloaded_options_kb' => round($autoload_size / 1024, 1),
        'autoloaded_options_count' => $autoload_count,
        'largest_autoloaded' => $big,
        'object_cache' => wp_using_ext_object_cache(),
        'php_version' => PHP_VERSION,
        'memory_limit' => defined('WP_MEMORY_LIMIT') ? WP_MEMORY_LIMIT : ini_get('memory_limit'),
        'issues' => $issues,
        'note' => 'Server-side performance signals. For lab Core Web Vitals (LCP/CLS/INP), run Google PageSpeed Insights on a public URL.',
    ));
}
function falcon_seo_rest_broken_links(WP_REST_Request $req) {
    $pid = (int) $req->get_param('post_id');
    $limit = $req->get_param('limit') ? min(100, max(1, (int) $req->get_param('limit'))) : 50;
    $posts = array();
    if ($pid) { $p = get_post($pid); if ($p) $posts[] = $p; }
    else {
        $scan = $req->get_param('scan_posts') ? min(100, (int) $req->get_param('scan_posts')) : 20;
        $q = new WP_Query(array('post_type' => array('post', 'page'), 'post_status' => 'publish', 'posts_per_page' => $scan));
        $posts = $q->posts;
    }
    $checked = array(); $broken = array(); $n = 0;
    foreach ($posts as $p) {
        if (preg_match_all('/href=["\']([^"\']+)["\']/i', $p->post_content, $m)) {
            foreach ($m[1] as $url) {
                if ($n >= $limit) break 2;
                if ($url === '' || $url[0] === '#' || strpos($url, 'mailto:') === 0 || strpos($url, 'tel:') === 0) continue;
                if (isset($checked[$url])) continue;
                $checked[$url] = 1; $n++;
                $abs = (strpos($url, 'http') === 0) ? $url : home_url($url);
                $resp = wp_remote_head($abs, array('timeout' => 7, 'redirection' => 3, 'sslverify' => false));
                $code = is_wp_error($resp) ? 0 : wp_remote_retrieve_response_code($resp);
                if ($code === 405 || $code === 0) {
                    $resp = wp_remote_get($abs, array('timeout' => 7, 'redirection' => 3, 'sslverify' => false));
                    $code = is_wp_error($resp) ? 0 : wp_remote_retrieve_response_code($resp);
                }
                if ($code === 0 || $code >= 400) {
                    $broken[] = array('post_id' => $p->ID, 'post_title' => get_the_title($p), 'url' => $url, 'status' => $code ? $code : 'unreachable');
                }
            }
        }
    }
    return rest_ensure_response(array('links_checked' => $n, 'broken_count' => count($broken), 'broken' => $broken));
}
function falcon_seo_rest_list_forms() {
    $out = array('cf7' => array(), 'wpforms' => array(), 'gravity' => array(), 'flamingo_available' => class_exists('Flamingo_Inbound_Message'));
    if (class_exists('WPCF7_ContactForm')) {
        foreach (WPCF7_ContactForm::find(array('posts_per_page' => 100)) as $f) $out['cf7'][] = array('id' => $f->id(), 'title' => $f->title());
    }
    if (post_type_exists('wpforms')) {
        $q = new WP_Query(array('post_type' => 'wpforms', 'posts_per_page' => 100, 'post_status' => 'publish'));
        foreach ($q->posts as $p) $out['wpforms'][] = array('id' => $p->ID, 'title' => get_the_title($p));
    }
    if (class_exists('GFAPI')) {
        foreach (GFAPI::get_forms() as $f) $out['gravity'][] = array('id' => $f['id'], 'title' => $f['title']);
    }
    return rest_ensure_response($out);
}
function falcon_seo_rest_form_submissions(WP_REST_Request $req) {
    $limit = $req->get_param('limit') ? min(100, max(1, (int) $req->get_param('limit'))) : 30;
    $form_id = $req->get_param('form_id');
    $rows = array(); $source = 'none';
    if (class_exists('GFAPI') && $form_id) {
        $source = 'gravity';
        foreach ((array) GFAPI::get_entries((int) $form_id, array(), null, array('page_size' => $limit)) as $e) $rows[] = $e;
    } elseif (function_exists('wpforms') && isset(wpforms()->entry) && is_object(wpforms()->entry) && method_exists(wpforms()->entry, 'get_entries')) {
        $source = 'wpforms';
        $args = array('number' => $limit);
        if ($form_id) $args['form_id'] = (int) $form_id;
        foreach ((array) wpforms()->entry->get_entries($args) as $e) $rows[] = (array) $e;
    } elseif (class_exists('Flamingo_Inbound_Message')) {
        $source = 'flamingo';
        $q = new WP_Query(array('post_type' => 'flamingo_inbound', 'posts_per_page' => $limit));
        foreach ($q->posts as $p) {
            $msg = new Flamingo_Inbound_Message($p);
            $rows[] = array('id' => $p->ID, 'subject' => $msg->subject, 'from' => $msg->from, 'date' => $p->post_date_gmt, 'fields' => $msg->fields);
        }
    }
    return rest_ensure_response(array('source' => $source, 'count' => count($rows), 'submissions' => $rows,
        'note' => $source === 'none' ? 'No readable form storage found. Contact Form 7 needs the free Flamingo plugin to store submissions; WPForms/Gravity Forms store entries in their paid versions.' : ''));
}

/* ---- Contact Form 7 control (free) ---- */
function falcon_seo_cf7_dto($cf7, $full = false) {
    $row = array(
        'id'        => $cf7->id(),
        'title'     => $cf7->title(),
        'shortcode' => '[contact-form-7 id="' . $cf7->id() . '" title="' . esc_attr($cf7->title()) . '"]',
    );
    if ($full) {
        $p = $cf7->get_properties();
        $row['form']     = isset($p['form']) ? $p['form'] : '';
        $row['mail']     = isset($p['mail']) ? $p['mail'] : array();
        $row['mail_2']   = isset($p['mail_2']) ? $p['mail_2'] : array();
        $row['messages'] = isset($p['messages']) ? $p['messages'] : array();
    }
    return $row;
}
function falcon_seo_rest_cf7_list() {
    if (!class_exists('WPCF7_ContactForm')) return new WP_Error('falcon_err', 'Contact Form 7 is not active on this site.', array('status' => 400));
    $out = array();
    foreach (WPCF7_ContactForm::find(array('posts_per_page' => 200)) as $f) $out[] = falcon_seo_cf7_dto($f);
    return rest_ensure_response(array('cf7_active' => true, 'count' => count($out), 'forms' => $out));
}
function falcon_seo_rest_cf7_get(WP_REST_Request $req) {
    if (!class_exists('WPCF7_ContactForm')) return new WP_Error('falcon_err', 'Contact Form 7 is not active on this site.', array('status' => 400));
    $cf7 = WPCF7_ContactForm::get_instance((int) $req['id']);
    if (!$cf7) return new WP_Error('falcon_not_found', 'Form not found.', array('status' => 404));
    return rest_ensure_response(falcon_seo_cf7_dto($cf7, true));
}
// Apply title/form/mail/messages from the request body onto a CF7 form object (form markup kept raw — it IS CF7 tags).
function falcon_seo_cf7_apply($cf7, $b) {
    if (isset($b['title']) && $b['title'] !== '' && method_exists($cf7, 'set_title')) $cf7->set_title(sanitize_text_field($b['title']));
    $props = $cf7->get_properties();
    if (isset($b['form']) && $b['form'] !== '') $props['form'] = (string) $b['form'];
    if (isset($b['mail']) && is_array($b['mail'])) $props['mail'] = array_merge((array) $props['mail'], $b['mail']);
    if (isset($b['mail_2']) && is_array($b['mail_2'])) $props['mail_2'] = array_merge((array) $props['mail_2'], $b['mail_2']);
    if (isset($b['messages']) && is_array($b['messages'])) $props['messages'] = array_merge((array) $props['messages'], $b['messages']);
    $cf7->set_properties($props);
    return $cf7;
}
function falcon_seo_rest_cf7_create(WP_REST_Request $req) {
    if (!class_exists('WPCF7_ContactForm')) return new WP_Error('falcon_err', 'Contact Form 7 is not active on this site.', array('status' => 400));
    $b = falcon_seo_body($req);
    $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
    if ($title === '') return new WP_Error('falcon_empty', 'title is required.', array('status' => 400));
    $cf7 = WPCF7_ContactForm::get_template(array('title' => $title));   // default name/email/subject/message template
    $cf7 = falcon_seo_cf7_apply($cf7, $b);
    $cf7->save();
    $cf7 = WPCF7_ContactForm::get_instance($cf7->id());
    falcon_seo_log($cf7->id(), 'cf7_create', array('title' => $title), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true) + falcon_seo_cf7_dto($cf7, true));
}
function falcon_seo_rest_cf7_update(WP_REST_Request $req) {
    if (!class_exists('WPCF7_ContactForm')) return new WP_Error('falcon_err', 'Contact Form 7 is not active on this site.', array('status' => 400));
    $cf7 = WPCF7_ContactForm::get_instance((int) $req['id']);
    if (!$cf7) return new WP_Error('falcon_not_found', 'Form not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $cf7 = falcon_seo_cf7_apply($cf7, $b);
    $cf7->save();
    falcon_seo_log($cf7->id(), 'cf7_update', array('id' => $cf7->id()), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true) + falcon_seo_cf7_dto($cf7, true));
}
function falcon_seo_rest_cf7_delete(WP_REST_Request $req) {
    if (!class_exists('WPCF7_ContactForm')) return new WP_Error('falcon_err', 'Contact Form 7 is not active on this site.', array('status' => 400));
    $cf7 = WPCF7_ContactForm::get_instance((int) $req['id']);
    if (!$cf7) return new WP_Error('falcon_not_found', 'Form not found.', array('status' => 404));
    $cf7->delete();
    falcon_seo_log((int) $req['id'], 'cf7_delete', array('id' => (int) $req['id']), falcon_seo_reason(falcon_seo_body($req)));
    return rest_ensure_response(array('deleted' => true, 'id' => (int) $req['id']));
}

/* ============================================================
 * Power tools (v1.11)
 * ============================================================ */

// Front-end maintenance mode (toggled via /maintenance). Admins/logged-in users bypass it.
add_action('template_redirect', function () {
    if (get_option('falcon_maintenance') && !is_user_logged_in()) {
        wp_die(esc_html(get_option('falcon_maintenance_msg') ?: 'This site is undergoing scheduled maintenance. Please check back soon.'),
            'Maintenance', array('response' => 503));
    }
});

function falcon_seo_admin_uid() {
    $a = get_users(array('role' => 'administrator', 'number' => 1, 'fields' => 'ID'));
    return !empty($a) ? (int) $a[0] : 0;
}

// Generic WP REST passthrough — runs an internal READ-ONLY request as an admin so any
// plugin's GET API is reachable. Restricted to GET: this impersonates an admin to satisfy
// other plugins' own capability checks, so it must not be usable to trigger writes on
// arbitrary third-party REST routes Falcon doesn't otherwise know about.
function falcon_seo_rest_proxy(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $method = isset($b['method']) ? strtoupper(sanitize_text_field($b['method'])) : 'GET';
    if ($method !== 'GET') {
        return new WP_Error('falcon_forbidden', 'The REST proxy is read-only; only GET is allowed. Use one of the dedicated write tools instead.', array('status' => 403));
    }
    $path = isset($b['path']) ? (string) $b['path'] : '';
    if ($path === '' || $path[0] !== '/') return new WP_Error('falcon_empty', 'path is required, e.g. /wp/v2/posts', array('status' => 400));
    $prev = get_current_user_id();
    $aid = falcon_seo_admin_uid();
    if ($aid) wp_set_current_user($aid);
    try {
        $r = new WP_REST_Request($method, $path);
        if (!empty($b['params']) && is_array($b['params'])) $r->set_query_params($b['params']);
        if (!empty($b['body']) && is_array($b['body'])) { $r->set_body_params($b['body']); $r->set_header('content-type', 'application/json'); }
        $resp = rest_do_request($r);
        $data = rest_get_server()->response_to_data($resp, false);
    } finally {
        wp_set_current_user($prev);
    }
    return rest_ensure_response(array('status' => $resp->get_status(), 'data' => $data));
}

// Read-only SQL (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH only, single statement).
function falcon_seo_rest_db_query(WP_REST_Request $req) {
    global $wpdb;
    $b = falcon_seo_body($req);
    $sql = isset($b['sql']) ? trim((string) $b['sql']) : '';
    if ($sql === '') return new WP_Error('falcon_empty', 'sql is required.', array('status' => 400));
    $first = strtoupper(strtok($sql, " \t\n\r("));
    if (!in_array($first, array('SELECT', 'SHOW', 'DESCRIBE', 'DESC', 'EXPLAIN', 'WITH'), true))
        return new WP_Error('falcon_ro', 'Only read-only queries (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH) are allowed.', array('status' => 400));
    if (preg_match('/\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|RENAME|LOCK|INTO\s+OUTFILE|SET)\b/i', $sql))
        return new WP_Error('falcon_ro', 'Write/DDL keywords are not allowed.', array('status' => 400));
    if (strpos(rtrim($sql, "; \t\n\r"), ';') !== false)
        return new WP_Error('falcon_ro', 'Only a single statement is allowed.', array('status' => 400));
    $rows = $wpdb->get_results($sql, ARRAY_A);
    if ($wpdb->last_error) return new WP_Error('falcon_sql', $wpdb->last_error, array('status' => 400));
    return rest_ensure_response(array('count' => is_array($rows) ? count($rows) : 0, 'rows' => $rows));
}

function falcon_seo_rest_get_option(WP_REST_Request $req) {
    $key = $req->get_param('key') ? sanitize_text_field($req->get_param('key')) : '';
    if ($key === '') return new WP_Error('falcon_empty', 'key is required.', array('status' => 400));
    return rest_ensure_response(array('key' => $key, 'value' => get_option($key)));
}
// Options that would let a caller take over the site or its delivery (switch the
// active theme/plugins, open registration as admin, redirect the whole domain, disable
// safety nets, etc.) rather than tweak a normal setting. Blocked outright.
function falcon_seo_option_denylist() {
    return array(
        'active_plugins', 'stylesheet', 'template', 'current_theme',
        'siteurl', 'home', 'blog_public',
        'users_can_register', 'default_role',
        'admin_email', 'recovery_mode_email',
        'auth_key', 'auth_salt', 'logged_in_key', 'logged_in_salt',
        'nonce_key', 'nonce_salt', 'secret', 'auth_cookie',
        'mu_plugins', 'active_sitewide_plugins', 'allowedthemes',
        'wp_user_roles', 'db_version', 'initial_db_version',
    );
}
function falcon_seo_rest_set_option(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $key = isset($b['key']) ? sanitize_text_field($b['key']) : '';
    if ($key === '' || !array_key_exists('value', $b)) return new WP_Error('falcon_empty', 'key and value are required.', array('status' => 400));
    if (in_array($key, falcon_seo_option_denylist(), true)) {
        return new WP_Error('falcon_forbidden', "'{$key}' controls core site/security config and can't be set through this tool.", array('status' => 403));
    }
    $ok = update_option($key, $b['value']);
    falcon_seo_log(0, 'set_option', array('key' => $key), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'key' => $key, 'changed' => (bool) $ok));
}

function falcon_seo_rest_duplicate_post(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $src = get_post($id);
    if (!$src) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $new = wp_insert_post(wp_slash(array(
        'post_title'   => (isset($b['title']) && $b['title'] !== '') ? sanitize_text_field($b['title']) : $src->post_title . ' (copy)',
        'post_content' => $src->post_content, 'post_excerpt' => $src->post_excerpt,
        'post_status'  => 'draft', 'post_type' => $src->post_type, 'post_author' => falcon_seo_default_author(),
    )), true);
    if (is_wp_error($new)) return new WP_Error('falcon_err', $new->get_error_message(), array('status' => 400));
    foreach (get_object_taxonomies($src->post_type) as $tax) {
        $terms = wp_get_object_terms($id, $tax, array('fields' => 'ids'));
        if (!is_wp_error($terms) && $terms) wp_set_object_terms($new, $terms, $tax);
    }
    foreach (get_post_meta($id) as $k => $vals) {
        if (in_array($k, array('_edit_lock', '_edit_last', '_wp_old_slug'), true)) continue;
        foreach ($vals as $v) add_post_meta($new, $k, maybe_unserialize($v));
    }
    falcon_seo_log($new, 'duplicate_post', array('from' => $id), falcon_seo_reason($b));
    return rest_ensure_response(array('duplicated' => true, 'new_post_id' => $new, 'edit_url' => admin_url("post.php?post={$new}&action=edit")));
}

function falcon_seo_rest_bulk_delete_posts(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $force = !empty($b['force']);
    if (!empty($b['ids']) && is_array($b['ids'])) {
        $ids = array_map('intval', $b['ids']);
    } else {
        $ids = get_posts(array('post_type' => isset($b['type']) ? sanitize_key($b['type']) : 'post',
            'post_status' => isset($b['status']) ? sanitize_key($b['status']) : 'any',
            'posts_per_page' => min(300, max(1, (int) ($b['max'] ?? 100))), 'fields' => 'ids'));
        if ($ids && empty($b['confirm'])) return new WP_Error('falcon_confirm', 'Filter-based bulk delete needs confirm=true.', array('status' => 400));
    }
    $done = array();
    foreach ($ids as $pid) { $r = $force ? wp_delete_post($pid, true) : wp_trash_post($pid); if ($r) $done[] = $pid; }
    falcon_seo_log(0, 'bulk_delete_posts', array('count' => count($done), 'force' => $force), falcon_seo_reason($b));
    return rest_ensure_response(array('deleted' => count($done), 'ids' => $done, 'permanent' => $force));
}

function falcon_seo_rest_cron(WP_REST_Request $req) {
    $events = _get_cron_array();
    $out = array();
    if (is_array($events)) foreach ($events as $ts => $hooks) foreach ($hooks as $hook => $sigs) foreach ($sigs as $sig) {
        $out[] = array('hook' => $hook, 'next_run_gmt' => gmdate('c', $ts), 'schedule' => isset($sig['schedule']) && $sig['schedule'] ? $sig['schedule'] : 'one-time');
    }
    return rest_ensure_response(array('count' => count($out), 'events' => $out));
}
function falcon_seo_rest_cron_run(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $hook = isset($b['hook']) ? sanitize_text_field($b['hook']) : '';
    if ($hook === '') return new WP_Error('falcon_empty', 'hook is required.', array('status' => 400));
    do_action_ref_array($hook, array());
    falcon_seo_log(0, 'cron_run', array('hook' => $hook), falcon_seo_reason($b));
    return rest_ensure_response(array('ran' => true, 'hook' => $hook));
}
function falcon_seo_rest_cron_clear(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $hook = isset($b['hook']) ? sanitize_text_field($b['hook']) : '';
    if ($hook === '') return new WP_Error('falcon_empty', 'hook is required.', array('status' => 400));
    $n = wp_clear_scheduled_hook($hook);
    falcon_seo_log(0, 'cron_clear', array('hook' => $hook, 'cleared' => $n), falcon_seo_reason($b));
    return rest_ensure_response(array('cleared' => (int) $n, 'hook' => $hook));
}

function falcon_seo_rest_maintenance(WP_REST_Request $req) {
    if ($req->get_method() === 'GET')
        return rest_ensure_response(array('enabled' => (bool) get_option('falcon_maintenance'), 'message' => get_option('falcon_maintenance_msg')));
    $b = falcon_seo_body($req);
    $on = !empty($b['enabled']);
    update_option('falcon_maintenance', $on ? 1 : 0);
    if (isset($b['message'])) update_option('falcon_maintenance_msg', sanitize_text_field($b['message']));
    falcon_seo_log(0, 'maintenance', array('enabled' => $on), falcon_seo_reason($b));
    return rest_ensure_response(array('enabled' => $on));
}

function falcon_seo_rest_comment_reply(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $parent = get_comment($id);
    if (!$parent) return new WP_Error('falcon_not_found', 'Comment not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $content = isset($b['content']) ? wp_kses_post($b['content']) : '';
    if ($content === '') return new WP_Error('falcon_empty', 'content is required.', array('status' => 400));
    $cid = wp_insert_comment(wp_slash(array(
        'comment_post_ID' => $parent->comment_post_ID, 'comment_parent' => $id, 'comment_content' => $content, 'comment_approved' => 1,
        'comment_author' => isset($b['author']) ? sanitize_text_field($b['author']) : get_bloginfo('name'),
        'comment_author_email' => isset($b['author_email']) ? sanitize_email($b['author_email']) : get_option('admin_email'),
        'user_id' => falcon_seo_default_author(),
    )));
    if (!$cid) return new WP_Error('falcon_err', 'Could not create reply.', array('status' => 400));
    falcon_seo_log($parent->comment_post_ID, 'comment_reply', array('parent' => $id), falcon_seo_reason($b));
    return rest_ensure_response(array('replied' => true, 'comment_id' => $cid));
}
function falcon_seo_rest_comment_bulk(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $action = isset($b['action']) ? sanitize_key($b['action']) : '';
    $ids = (!empty($b['ids']) && is_array($b['ids'])) ? array_map('intval', $b['ids']) : array();
    if (!$ids) return new WP_Error('falcon_empty', 'ids[] is required.', array('status' => 400));
    if (!in_array($action, array('approve', 'unapprove', 'spam', 'trash', 'delete'), true))
        return new WP_Error('falcon_bad', 'action must be approve|unapprove|spam|trash|delete.', array('status' => 400));
    $done = 0;
    foreach ($ids as $cid) {
        if ($action === 'approve') wp_set_comment_status($cid, 'approve');
        elseif ($action === 'unapprove') wp_set_comment_status($cid, 'hold');
        elseif ($action === 'spam') wp_spam_comment($cid);
        elseif ($action === 'trash') wp_trash_comment($cid);
        elseif ($action === 'delete') wp_delete_comment($cid, true);
        $done++;
    }
    falcon_seo_log(0, 'comment_bulk', array('action' => $action, 'count' => $done), falcon_seo_reason($b));
    return rest_ensure_response(array('action' => $action, 'affected' => $done));
}

function falcon_seo_rest_media_replace(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (get_post_type($id) !== 'attachment') return new WP_Error('falcon_not_found', 'Attachment not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $url = isset($b['url']) ? esc_url_raw($b['url']) : '';
    if ($url === '') return new WP_Error('falcon_empty', 'url is required.', array('status' => 400));
    falcon_seo_require_media();
    $tmp = download_url($url);
    if (is_wp_error($tmp)) return new WP_Error('falcon_err', $tmp->get_error_message(), array('status' => 400));
    $dest = get_attached_file($id);
    if (!$dest) { @unlink($tmp); return new WP_Error('falcon_err', 'Original file path not found.', array('status' => 400)); }
    if (!@copy($tmp, $dest)) { @unlink($tmp); return new WP_Error('falcon_err', 'Could not write replacement file.', array('status' => 400)); }
    @unlink($tmp);
    wp_update_attachment_metadata($id, wp_generate_attachment_metadata($id, $dest));
    falcon_seo_log($id, 'media_replace', array(), falcon_seo_reason($b));
    return rest_ensure_response(array('replaced' => true, 'id' => $id, 'url' => wp_get_attachment_url($id)));
}
function falcon_seo_rest_bulk_alt(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $items = (!empty($b['items']) && is_array($b['items'])) ? $b['items'] : array();
    if (!$items) return new WP_Error('falcon_empty', 'items:[{id, alt}] is required.', array('status' => 400));
    $done = 0;
    foreach ($items as $it) {
        $id = (int) ($it['id'] ?? 0);
        if (!$id) continue;
        update_post_meta($id, '_wp_attachment_image_alt', sanitize_text_field($it['alt'] ?? ''));
        $done++;
    }
    falcon_seo_log(0, 'bulk_set_image_alt', array('count' => $done), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => $done));
}

function falcon_seo_rest_block_patterns(WP_REST_Request $req) {
    if (!class_exists('WP_Block_Patterns_Registry')) return rest_ensure_response(array('count' => 0, 'patterns' => array()));
    $out = array();
    foreach (WP_Block_Patterns_Registry::get_instance()->get_all_registered() as $p) {
        $out[] = array('name' => $p['name'], 'title' => isset($p['title']) ? $p['title'] : '', 'categories' => isset($p['categories']) ? $p['categories'] : array());
    }
    return rest_ensure_response(array('count' => count($out), 'patterns' => $out));
}
function falcon_seo_rest_insert_pattern(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $name = isset($b['pattern']) ? sanitize_text_field($b['pattern']) : '';
    if (!class_exists('WP_Block_Patterns_Registry') || !WP_Block_Patterns_Registry::get_instance()->is_registered($name))
        return new WP_Error('falcon_not_found', 'Pattern not registered: ' . $name, array('status' => 404));
    $p = WP_Block_Patterns_Registry::get_instance()->get_registered($name);
    $content = isset($p['content']) ? $p['content'] : '';
    $post = get_post($id);
    $new = !empty($b['append']) ? ($post->post_content . "\n\n" . $content) : $content;
    wp_update_post(array('ID' => $id, 'post_content' => wp_slash($new)));
    falcon_seo_log($id, 'insert_pattern', array('pattern' => $name), falcon_seo_reason($b));
    return rest_ensure_response(array('inserted' => true, 'post_id' => $id, 'pattern' => $name));
}

function falcon_seo_rest_roles(WP_REST_Request $req) {
    $out = array();
    foreach (wp_roles()->roles as $slug => $r) {
        $out[] = array('slug' => $slug, 'name' => $r['name'], 'capabilities' => array_keys(array_filter($r['capabilities'])));
    }
    return rest_ensure_response(array('count' => count($out), 'roles' => $out));
}
function falcon_seo_rest_role_caps(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $role = get_role(isset($b['role']) ? sanitize_key($b['role']) : '');
    if (!$role) return new WP_Error('falcon_not_found', 'Role not found.', array('status' => 404));
    $added = array(); $removed = array();
    foreach ((array) ($b['add'] ?? array()) as $c) { $role->add_cap(sanitize_key($c)); $added[] = $c; }
    foreach ((array) ($b['remove'] ?? array()) as $c) { $role->remove_cap(sanitize_key($c)); $removed[] = $c; }
    falcon_seo_log(0, 'role_caps', array('role' => $role->name, 'added' => $added, 'removed' => $removed), falcon_seo_reason($b));
    return rest_ensure_response(array('added' => $added, 'removed' => $removed));
}
function falcon_seo_rest_role_create(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $slug = isset($b['slug']) ? sanitize_key($b['slug']) : '';
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if ($slug === '' || $name === '') return new WP_Error('falcon_empty', 'slug and name are required.', array('status' => 400));
    $caps = array();
    foreach ((array) ($b['capabilities'] ?? array('read')) as $c) $caps[sanitize_key($c)] = true;
    if (!add_role($slug, $name, $caps)) return new WP_Error('falcon_err', 'Role already exists or could not be created.', array('status' => 400));
    falcon_seo_log(0, 'role_create', array('role' => $slug), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'role' => $slug));
}

function falcon_seo_rest_flush_rewrite(WP_REST_Request $req) {
    flush_rewrite_rules(false);
    return rest_ensure_response(array('flushed' => true));
}
function falcon_seo_rest_debug_log(WP_REST_Request $req) {
    $file = WP_CONTENT_DIR . '/debug.log';
    $lines = $req->get_param('lines') ? min(500, max(1, (int) $req->get_param('lines'))) : 100;
    $tail = '';
    if (file_exists($file)) { $all = @file($file); if (is_array($all)) $tail = implode('', array_slice($all, -$lines)); }
    return rest_ensure_response(array('exists' => file_exists($file), 'wp_debug' => (defined('WP_DEBUG') && WP_DEBUG), 'tail' => $tail));
}

/* ---- WooCommerce depth ---- */
function falcon_seo_rest_wc_customers(WP_REST_Request $req) {
    if (!class_exists('WooCommerce')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $per = $req->get_param('per_page') ? min(100, max(1, (int) $req->get_param('per_page'))) : 20;
    $q = new WP_User_Query(array('role' => 'customer', 'number' => $per, 'paged' => $req->get_param('page') ? max(1, (int) $req->get_param('page')) : 1));
    $out = array();
    foreach ($q->get_results() as $u) {
        $out[] = array('id' => $u->ID, 'email' => $u->user_email,
            'name' => trim($u->first_name . ' ' . $u->last_name) ?: $u->display_name,
            'orders' => wc_get_customer_order_count($u->ID), 'total_spent' => (float) wc_get_customer_total_spent($u->ID));
    }
    return rest_ensure_response(array('count' => count($out), 'customers' => $out));
}
function falcon_seo_rest_wc_variations(WP_REST_Request $req) {
    if (!function_exists('wc_get_product')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $product = wc_get_product((int) $req['id']);
    if (!$product) return new WP_Error('falcon_not_found', 'Product not found.', array('status' => 404));
    $out = array();
    foreach ($product->get_children() as $vid) {
        $v = wc_get_product($vid);
        if (!$v) continue;
        $out[] = array('id' => $vid, 'sku' => $v->get_sku(), 'price' => $v->get_price(), 'regular_price' => $v->get_regular_price(),
            'sale_price' => $v->get_sale_price(), 'stock' => $v->get_stock_quantity(), 'attributes' => $v->get_attributes());
    }
    return rest_ensure_response(array('product_id' => (int) $req['id'], 'count' => count($out), 'variations' => $out));
}
function falcon_seo_rest_wc_update_variation(WP_REST_Request $req) {
    if (!function_exists('wc_get_product')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $v = wc_get_product((int) $req['id']);
    if (!$v || !$v->is_type('variation')) return new WP_Error('falcon_not_found', 'Variation not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    if (isset($b['regular_price'])) $v->set_regular_price((string) $b['regular_price']);
    if (isset($b['sale_price'])) $v->set_sale_price((string) $b['sale_price']);
    if (isset($b['sku'])) $v->set_sku(sanitize_text_field($b['sku']));
    if (isset($b['stock_quantity'])) { $v->set_manage_stock(true); $v->set_stock_quantity((int) $b['stock_quantity']); }
    $v->save();
    falcon_seo_log((int) $req['id'], 'wc_update_variation', array(), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'variation_id' => (int) $req['id']));
}
function falcon_seo_rest_wc_shipping_zones(WP_REST_Request $req) {
    if (!class_exists('WC_Shipping_Zones')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $out = array();
    foreach (WC_Shipping_Zones::get_zones() as $z) {
        $methods = array();
        foreach ($z['shipping_methods'] as $m) $methods[] = array('id' => $m->id, 'title' => $m->get_title(), 'enabled' => $m->is_enabled());
        $out[] = array('id' => $z['id'], 'name' => $z['zone_name'], 'regions' => wp_list_pluck($z['zone_locations'], 'code'), 'methods' => $methods);
    }
    return rest_ensure_response(array('count' => count($out), 'zones' => $out));
}
function falcon_seo_rest_wc_tax_rates(WP_REST_Request $req) {
    global $wpdb;
    if (!class_exists('WooCommerce')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $rows = $wpdb->get_results("SELECT tax_rate_id, tax_rate_country, tax_rate_state, tax_rate, tax_rate_name, tax_rate_class, tax_rate_priority FROM {$wpdb->prefix}woocommerce_tax_rates LIMIT 300", ARRAY_A);
    return rest_ensure_response(array('count' => is_array($rows) ? count($rows) : 0, 'tax_rates' => $rows));
}
function falcon_seo_rest_wc_webhooks(WP_REST_Request $req) {
    if (!function_exists('wc_get_webhook') || !class_exists('WC_Data_Store')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $out = array();
    foreach (WC_Data_Store::load('webhook')->get_webhooks_ids() as $wid) {
        $wh = wc_get_webhook($wid);
        if (!$wh) continue;
        $out[] = array('id' => $wid, 'name' => $wh->get_name(), 'topic' => $wh->get_topic(), 'delivery_url' => $wh->get_delivery_url(), 'status' => $wh->get_status());
    }
    return rest_ensure_response(array('count' => count($out), 'webhooks' => $out));
}
function falcon_seo_rest_wc_create_webhook(WP_REST_Request $req) {
    if (!class_exists('WC_Webhook')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $b = falcon_seo_body($req);
    $topic = isset($b['topic']) ? sanitize_text_field($b['topic']) : '';
    $url = isset($b['delivery_url']) ? esc_url_raw($b['delivery_url']) : '';
    if ($topic === '' || $url === '') return new WP_Error('falcon_empty', 'topic and delivery_url are required.', array('status' => 400));
    $wh = new WC_Webhook();
    $wh->set_name(isset($b['name']) ? sanitize_text_field($b['name']) : $topic);
    $wh->set_topic($topic);
    $wh->set_delivery_url($url);
    $wh->set_status(isset($b['status']) ? sanitize_key($b['status']) : 'active');
    $wh->set_user_id(falcon_seo_default_author());
    $wh->save();
    falcon_seo_log(0, 'wc_create_webhook', array('topic' => $topic), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'id' => $wh->get_id()));
}

/* ============================================================
 * Security suite (v1.12) — whole-site checks
 * ============================================================ */

// Outdated + (proxy for) vulnerable core, plugins, themes. Outdated = the top real-world attack vector.
function falcon_seo_sec_vulnerabilities() {
    if (!function_exists('get_plugins')) require_once ABSPATH . 'wp-admin/includes/plugin.php';
    @wp_update_plugins(); @wp_update_themes();
    global $wp_version;
    $out = array('core' => array(), 'plugins' => array(), 'themes' => array());
    $core = get_site_transient('update_core');
    $core_latest = $wp_version;
    if ($core && !empty($core->updates)) foreach ($core->updates as $u) { if (isset($u->response) && $u->response === 'upgrade') { $core_latest = $u->current; break; } }
    $out['core'] = array('installed' => $wp_version, 'latest' => $core_latest, 'update_available' => version_compare($wp_version, $core_latest, '<'));
    $upd = get_site_transient('update_plugins');
    foreach (get_plugins() as $file => $p) {
        $out['plugins'][] = array('name' => $p['Name'], 'slug' => dirname($file), 'installed' => $p['Version'],
            'latest' => isset($upd->response[$file]->new_version) ? $upd->response[$file]->new_version : $p['Version'],
            'update_available' => isset($upd->response[$file]), 'active' => is_plugin_active($file));
    }
    $tupd = get_site_transient('update_themes');
    foreach (wp_get_themes() as $slug => $t) {
        $out['themes'][] = array('name' => $t->get('Name'), 'slug' => $slug, 'installed' => $t->get('Version'),
            'update_available' => isset($tupd->response[$slug]));
    }
    $warn = array();
    $po = count(array_filter($out['plugins'], function ($p) { return $p['update_available']; }));
    if ($po) $warn[] = "$po plugin(s) are outdated — update them (outdated code is the #1 attack vector).";
    if ($out['core']['update_available']) $warn[] = 'WordPress core is outdated.';
    return rest_ensure_response(array_merge($out, array('warnings' => $warn,
        'note' => 'For CVE-level matching, pass a WPScan API key on the connector side.')));
}

function falcon_seo_sec_file_integrity() {
    $content = WP_CONTENT_DIR;
    $cutoff = time() - 7 * 86400;
    $f = array('recent_php' => array(), 'php_in_uploads' => array(), 'world_writable' => array());
    $n = 0;
    try {
        $rii = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($content, FilesystemIterator::SKIP_DOTS));
        foreach ($rii as $file) {
            if ($n++ > 20000) break;
            $path = $file->getPathname();
            $rel = str_replace($content, 'wp-content', $path);
            if (substr($path, -4) === '.php') {
                if ($file->getMTime() > $cutoff && count($f['recent_php']) < 100) $f['recent_php'][] = array('file' => $rel, 'modified' => gmdate('c', $file->getMTime()));
                if (strpos($path, DIRECTORY_SEPARATOR . 'uploads' . DIRECTORY_SEPARATOR) !== false && count($f['php_in_uploads']) < 100) $f['php_in_uploads'][] = $rel;
            }
            if ($file->isFile() && ($file->getPerms() & 0x0002) && count($f['world_writable']) < 100) $f['world_writable'][] = $rel;
        }
    } catch (\Throwable $e) { /* ignore traversal errors */ }
    $warn = array();
    if ($f['php_in_uploads']) $warn[] = count($f['php_in_uploads']) . ' PHP file(s) found in uploads/ — uploads should never contain PHP (likely malware).';
    if (count($f['recent_php']) > 20) $warn[] = count($f['recent_php']) . ' PHP files changed in the last 7 days — review if you did not expect changes.';
    if ($f['world_writable']) $warn[] = count($f['world_writable']) . ' world-writable file(s) under wp-content.';
    return rest_ensure_response(array('window_days' => 7, 'files_scanned' => $n, 'findings' => $f, 'warnings' => $warn));
}

function falcon_seo_sec_permissions() {
    $targets = array('wp-config.php' => ABSPATH . 'wp-config.php', '.htaccess' => ABSPATH . '.htaccess',
        'wp-content' => WP_CONTENT_DIR, 'uploads' => wp_get_upload_dir()['basedir'], 'root' => ABSPATH);
    $rows = array(); $warn = array();
    foreach ($targets as $name => $path) {
        if (!file_exists($path)) continue;
        $perms = substr(sprintf('%o', fileperms($path)), -3);
        $ww = (bool) (fileperms($path) & 0x0002);
        $rows[$name] = array('perms' => $perms, 'world_writable' => $ww);
        if ($ww) $warn[] = "$name is world-writable ($perms) — tighten it.";
    }
    if (isset($rows['wp-config.php']) && (int) $rows['wp-config.php']['perms'] > 644)
        $warn[] = 'wp-config.php is ' . $rows['wp-config.php']['perms'] . ' — tighten to 600 or 640.';
    return rest_ensure_response(array('permissions' => $rows, 'warnings' => $warn));
}

function falcon_seo_sec_hardening() {
    global $wpdb, $wp_version;
    $c = array();
    $c['file_editing_disabled'] = defined('DISALLOW_FILE_EDIT') && DISALLOW_FILE_EDIT;
    $c['debug_off'] = !(defined('WP_DEBUG') && WP_DEBUG);
    $c['ssl_admin'] = defined('FORCE_SSL_ADMIN') && FORCE_SSL_ADMIN;
    $c['xmlrpc_enabled'] = (bool) apply_filters('xmlrpc_enabled', true);
    $c['registration_open'] = (bool) get_option('users_can_register');
    $c['default_role'] = get_option('default_role');
    $c['table_prefix_default'] = ($wpdb->prefix === 'wp_');
    $c['readme_html_present'] = file_exists(ABSPATH . 'readme.html');
    $salts = array('AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY', 'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT');
    $ok = true; $hashes = array();
    foreach ($salts as $s) { $v = defined($s) ? constant($s) : ''; if (!$v || strpos($v, 'put your unique phrase here') !== false) $ok = false; $hashes[] = md5((string) $v); }
    $c['salts_set_unique'] = $ok && count(array_unique($hashes)) === count($hashes);
    $warn = array();
    if (!$c['file_editing_disabled']) $warn[] = 'Define DISALLOW_FILE_EDIT to block the in-dashboard theme/plugin editor.';
    if (!$c['debug_off']) $warn[] = 'WP_DEBUG is on — turn it off in production.';
    if ($c['xmlrpc_enabled']) $warn[] = 'XML-RPC is enabled — disable if unused (brute-force/pingback vector).';
    if ($c['registration_open'] && $c['default_role'] === 'administrator') $warn[] = 'CRITICAL: open registration defaults to administrator.';
    if ($c['table_prefix_default']) $warn[] = 'Default table prefix wp_.';
    if (!$c['salts_set_unique']) $warn[] = 'Auth salts missing, default, or duplicated — regenerate them.';
    if ($c['readme_html_present']) $warn[] = 'readme.html present — it leaks the WP version; delete it.';
    return rest_ensure_response(array('wp_version' => $wp_version, 'checks' => $c, 'warnings' => $warn));
}

function falcon_seo_sec_logins() {
    $rows = array(); $sessions = 0;
    foreach (get_users(array('role' => 'administrator')) as $u) {
        $st = get_user_meta($u->ID, 'session_tokens', true);
        $cnt = is_array($st) ? count($st) : 0;
        $sessions += $cnt;
        $rows[] = array('id' => $u->ID, 'login' => $u->user_login, 'email' => $u->user_email, 'registered' => $u->user_registered,
            'active_sessions' => $cnt, 'username_is_guessable' => in_array(strtolower($u->user_login), array('admin', 'administrator', 'root', 'test'), true));
    }
    $warn = array();
    foreach ($rows as $r) if ($r['username_is_guessable']) $warn[] = "Administrator uses a guessable username '{$r['login']}'.";
    if (count($rows) > 3) $warn[] = 'Many administrator accounts (' . count($rows) . ') — keep admins to a minimum.';
    return rest_ensure_response(array('admin_count' => count($rows), 'active_admin_sessions' => $sessions, 'admins' => $rows, 'warnings' => $warn));
}
function falcon_seo_sec_force_logout(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $count = 0;
    if (class_exists('WP_Session_Tokens')) {
        foreach (get_users(array('fields' => 'ID')) as $uid) { WP_Session_Tokens::get_instance($uid)->destroy_all(); $count++; }
    }
    falcon_seo_log(0, 'force_logout_all', array('users' => $count), falcon_seo_reason($b));
    return rest_ensure_response(array('logged_out_users' => $count));
}

function falcon_seo_sec_suspicious() {
    global $wpdb;
    $out = array('suspicious_options' => array(), 'suspicious_cron' => array(), 'recent_admins' => array());
    $rows = $wpdb->get_results("SELECT option_name, option_value FROM {$wpdb->options} WHERE autoload='yes'", ARRAY_A);
    if (is_array($rows)) foreach ($rows as $r) {
        if (preg_match('/(base64_decode|eval\s*\(|<script|gzinflate|str_rot13)/i', (string) $r['option_value']) && count($out['suspicious_options']) < 50)
            $out['suspicious_options'][] = array('option' => $r['option_name'], 'snippet' => substr((string) $r['option_value'], 0, 120));
    }
    $events = _get_cron_array();
    if (is_array($events)) foreach ($events as $ts => $hooks) foreach ($hooks as $hook => $x)
        if (preg_match('/(eval|base64|gzinflate|assert)/i', $hook)) $out['suspicious_cron'][] = $hook;
    $cut = gmdate('Y-m-d H:i:s', time() - 30 * 86400);
    foreach (get_users(array('role' => 'administrator')) as $u)
        if ($u->user_registered > $cut) $out['recent_admins'][] = array('login' => $u->user_login, 'registered' => $u->user_registered);
    $warn = array();
    if ($out['suspicious_options']) $warn[] = count($out['suspicious_options']) . ' autoloaded option(s) contain code-like patterns — inspect for injection.';
    if ($out['suspicious_cron']) $warn[] = 'Suspicious cron hook name(s) detected.';
    if ($out['recent_admins']) $warn[] = count($out['recent_admins']) . ' admin account(s) created in the last 30 days.';
    return rest_ensure_response(array_merge($out, array('warnings' => $warn)));
}

function falcon_seo_sec_secrets() {
    $salts = array('AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY', 'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT');
    $missing = array(); $hashes = array();
    foreach ($salts as $s) { $v = defined($s) ? constant($s) : ''; if (!$v || strpos($v, 'put your unique phrase here') !== false) $missing[] = $s; else $hashes[] = md5($v); }
    $cfg = ABSPATH . 'wp-config.php';
    $perms = file_exists($cfg) ? substr(sprintf('%o', fileperms($cfg)), -3) : null;
    $warn = array();
    if ($missing) $warn[] = 'Auth salts missing/default: ' . implode(', ', $missing) . ' — regenerate via api.wordpress.org/secret-key.';
    if (count(array_unique($hashes)) < count($hashes)) $warn[] = 'Some auth salts are duplicated.';
    if ($perms && (int) $perms > 644) $warn[] = "wp-config.php perms are $perms — tighten to 600/640.";
    return rest_ensure_response(array('salts_missing_or_default' => $missing, 'wpconfig_perms' => $perms, 'warnings' => $warn));
}

function falcon_seo_sec_mixed_content() {
    global $wpdb;
    if (strpos(home_url(), 'https://') !== 0)
        return rest_ensure_response(array('https' => false, 'warnings' => array('Site is not served over HTTPS — enable SSL first.'), 'posts' => array()));
    $host = parse_url(home_url(), PHP_URL_HOST);
    $like = '%http://' . $wpdb->esc_like($host) . '%';
    $rows = $wpdb->get_results($wpdb->prepare("SELECT ID, post_title FROM {$wpdb->posts} WHERE post_status='publish' AND post_content LIKE %s LIMIT 100", $like), ARRAY_A);
    $warn = array();
    if ($rows) $warn[] = count($rows) . " published post(s) reference insecure http://$host URLs (mixed content breaks the padlock).";
    return rest_ensure_response(array('https' => true, 'count' => is_array($rows) ? count($rows) : 0, 'posts' => $rows, 'warnings' => $warn));
}

// One-shot: run every server-side check, score it, return a prioritized report.
function falcon_seo_sec_audit(WP_REST_Request $req) {
    $sections = array(
        'vulnerabilities' => falcon_seo_sec_vulnerabilities()->get_data(),
        'file_integrity'  => falcon_seo_sec_file_integrity()->get_data(),
        'permissions'     => falcon_seo_sec_permissions()->get_data(),
        'hardening'       => falcon_seo_sec_hardening()->get_data(),
        'logins'          => falcon_seo_sec_logins()->get_data(),
        'suspicious'      => falcon_seo_sec_suspicious()->get_data(),
        'secrets'         => falcon_seo_sec_secrets()->get_data(),
        'mixed_content'   => falcon_seo_sec_mixed_content()->get_data(),
    );
    $issues = array();
    foreach ($sections as $area => $s) if (!empty($s['warnings'])) foreach ($s['warnings'] as $w) $issues[] = array('area' => $area, 'issue' => $w);
    $score = 100 - min(60, count($issues) * 7);
    if (!empty($sections['suspicious']['suspicious_options']) || !empty($sections['file_integrity']['findings']['php_in_uploads'])) $score -= 25;
    if (!empty($sections['vulnerabilities']['core']['update_available'])) $score -= 8;
    $score = max(0, $score);
    $grade = $score >= 85 ? 'A' : ($score >= 70 ? 'B' : ($score >= 50 ? 'C' : ($score >= 30 ? 'D' : 'F')));
    return rest_ensure_response(array('score' => $score, 'grade' => $grade, 'issue_count' => count($issues), 'issues' => $issues, 'sections' => $sections));
}

/* ============================================================
 * Themes (advanced / FSE)
 * ============================================================ */
function falcon_seo_rest_list_templates(WP_REST_Request $req) {
    if (!function_exists('get_block_templates')) return new WP_Error('falcon_err', 'Block templates need WordPress 5.9+ and a block theme.', array('status' => 400));
    $type = $req->get_param('type') === 'wp_template_part' ? 'wp_template_part' : 'wp_template';
    $rows = array();
    foreach (get_block_templates(array(), $type) as $t) {
        $rows[] = array('id' => $t->id, 'slug' => $t->slug, 'title' => is_object($t->title ?? null) ? $t->title->rendered : ($t->title ?? $t->slug),
            'source' => $t->source, 'theme' => $t->theme);
    }
    return rest_ensure_response(array('type' => $type, 'count' => count($rows), 'templates' => $rows));
}
function falcon_seo_rest_get_template(WP_REST_Request $req) {
    if (!function_exists('get_block_template')) return new WP_Error('falcon_err', 'Block templates need WordPress 5.9+ and a block theme.', array('status' => 400));
    $id = $req->get_param('id') ? sanitize_text_field($req->get_param('id')) : '';
    $type = $req->get_param('type') === 'wp_template_part' ? 'wp_template_part' : 'wp_template';
    if (!$id) return new WP_Error('falcon_empty', 'id is required (e.g. theme//index).', array('status' => 400));
    $t = get_block_template($id, $type);
    if (!$t) return new WP_Error('falcon_not_found', 'Template not found.', array('status' => 404));
    return rest_ensure_response(array('id' => $t->id, 'slug' => $t->slug, 'type' => $type, 'content' => $t->content));
}
function falcon_seo_rest_edit_template(WP_REST_Request $req) {
    if (!function_exists('get_block_templates')) return new WP_Error('falcon_err', 'Block templates need WordPress 5.9+ and a block theme.', array('status' => 400));
    $b = falcon_seo_body($req);
    $slug = isset($b['slug']) ? sanitize_title($b['slug']) : '';
    $type = ($b['type'] ?? 'wp_template') === 'wp_template_part' ? 'wp_template_part' : 'wp_template';
    if ($slug === '' || !isset($b['content'])) return new WP_Error('falcon_empty', 'slug and content are required.', array('status' => 400));
    $theme = get_stylesheet();
    $existing = get_posts(array('post_type' => $type, 'name' => $slug, 'numberposts' => 1, 'post_status' => 'any',
        'tax_query' => array(array('taxonomy' => 'wp_theme', 'field' => 'name', 'terms' => $theme))));
    if ($existing) {
        wp_update_post(array('ID' => $existing[0]->ID, 'post_content' => $b['content']));
        $pid = $existing[0]->ID;
    } else {
        $pid = wp_insert_post(array('post_type' => $type, 'post_name' => $slug, 'post_title' => $slug,
            'post_status' => 'publish', 'post_content' => $b['content']), true);
        if (is_wp_error($pid)) return new WP_Error('falcon_err', $pid->get_error_message(), array('status' => 400));
        wp_set_object_terms($pid, $theme, 'wp_theme');
    }
    falcon_seo_log($pid, 'edit_block_template', array('slug' => $slug, 'type' => $type), falcon_seo_reason($b));
    return rest_ensure_response(array('saved' => true, 'id' => "{$theme}//{$slug}", 'type' => $type));
}
function falcon_seo_global_styles_pid() {
    if (!class_exists('WP_Theme_JSON_Resolver')) return 0;
    if (method_exists('WP_Theme_JSON_Resolver', 'get_user_global_styles_post_id')) {
        return (int) WP_Theme_JSON_Resolver::get_user_global_styles_post_id();
    }
    return 0;
}
function falcon_seo_rest_get_global_styles() {
    $pid = falcon_seo_global_styles_pid();
    if (!$pid) return new WP_Error('falcon_err', 'Global styles (theme.json) need a block theme on WordPress 5.9+.', array('status' => 400));
    $content = get_post($pid)->post_content;
    return rest_ensure_response(array('post_id' => $pid, 'global_styles' => $content ? json_decode($content, true) : array()));
}
function falcon_seo_rest_set_global_styles(WP_REST_Request $req) {
    $pid = falcon_seo_global_styles_pid();
    if (!$pid) return new WP_Error('falcon_err', 'Global styles (theme.json) need a block theme on WordPress 5.9+.', array('status' => 400));
    $b = falcon_seo_body($req);
    $styles = $b['global_styles'] ?? null;
    if ($styles === null) return new WP_Error('falcon_empty', 'global_styles (object or JSON string) is required.', array('status' => 400));
    if (is_string($styles)) { $styles = json_decode($styles, true); if ($styles === null) return new WP_Error('falcon_err', 'global_styles is not valid JSON.', array('status' => 400)); }
    if (empty($styles['version'])) $styles['version'] = 2;
    if (!empty($b['merge'])) {
        $cur = json_decode(get_post($pid)->post_content, true);
        if (is_array($cur)) $styles = array_replace_recursive($cur, $styles);
    }
    wp_update_post(array('ID' => $pid, 'post_content' => wp_json_encode($styles)));
    falcon_seo_log($pid, 'set_global_styles', array('keys' => array_keys($styles)), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $pid));
}

/* ============================================================
 * WooCommerce — bulk & more
 * ============================================================ */
function falcon_seo_rest_wc_bulk_update(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $ids = array();
    if (!empty($b['ids']) && is_array($b['ids'])) { $ids = array_map('intval', $b['ids']); }
    else {
        $args = array('limit' => isset($b['limit']) ? (int) $b['limit'] : 200, 'return' => 'ids');
        if (!empty($b['filter']['category'])) $args['category'] = array(sanitize_title($b['filter']['category']));
        if (!empty($b['filter']['status'])) $args['status'] = sanitize_key($b['filter']['status']);
        if (!empty($b['filter']['stock_status'])) $args['stock_status'] = sanitize_key($b['filter']['stock_status']);
        $ids = wc_get_products($args);
    }
    $set = isset($b['set']) && is_array($b['set']) ? $b['set'] : array();
    $adj = isset($b['price_adjust']) && is_array($b['price_adjust']) ? $b['price_adjust'] : null;
    $done = 0;
    foreach ($ids as $pid) {
        $p = wc_get_product($pid); if (!$p) continue;
        if (isset($set['regular_price'])) $p->set_regular_price((string) $set['regular_price']);
        if (isset($set['sale_price'])) $p->set_sale_price((string) $set['sale_price']);
        if (isset($set['status'])) $p->set_status(sanitize_key($set['status']));
        if (isset($set['stock_status'])) $p->set_stock_status(sanitize_key($set['stock_status']));
        if (isset($set['stock_quantity'])) { $p->set_manage_stock(true); $p->set_stock_quantity((int) $set['stock_quantity']); }
        if ($adj && isset($adj['value'])) {
            $field = ($adj['field'] ?? 'regular') === 'sale' ? 'sale' : 'regular';
            $cur = (float) ($field === 'sale' ? $p->get_sale_price() : $p->get_regular_price());
            $val = (float) $adj['value'];
            $new = ($adj['type'] ?? 'percent') === 'percent' ? $cur + ($cur * $val / 100) : $cur + $val;
            $new = max(0, round($new, 2));
            if ($field === 'sale') $p->set_sale_price((string) $new); else $p->set_regular_price((string) $new);
        }
        if (!empty($set['category'])) wp_set_object_terms($pid, is_array($set['category']) ? $set['category'] : array($set['category']), 'product_cat', true);
        $p->save(); $done++;
    }
    falcon_seo_log(0, 'wc_bulk_update', array('count' => $done), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => $done, 'product_ids' => array_values($ids)));
}
function falcon_seo_rest_wc_bulk_create(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $items = (!empty($b['products']) && is_array($b['products'])) ? $b['products'] : array();
    if (!$items) return new WP_Error('falcon_empty', 'products array is required.', array('status' => 400));
    $created = array();
    foreach ($items as $it) {
        $name = isset($it['name']) ? sanitize_text_field($it['name']) : '';
        if ($name === '') continue;
        $p = new WC_Product_Simple();
        $p->set_name($name);
        if (isset($it['regular_price'])) $p->set_regular_price((string) $it['regular_price']);
        if (isset($it['sale_price'])) $p->set_sale_price((string) $it['sale_price']);
        if (isset($it['description'])) $p->set_description(wp_kses_post($it['description']));
        if (isset($it['sku'])) $p->set_sku(sanitize_text_field($it['sku']));
        if (isset($it['stock_quantity'])) { $p->set_manage_stock(true); $p->set_stock_quantity((int) $it['stock_quantity']); }
        $p->set_status(in_array(($it['status'] ?? 'publish'), array('publish', 'draft', 'pending', 'private'), true) ? $it['status'] : 'publish');
        $id = $p->save();
        if (!empty($it['categories'])) wp_set_object_terms($id, is_array($it['categories']) ? $it['categories'] : array_map('trim', explode(',', $it['categories'])), 'product_cat');
        if (!empty($it['image_url'])) { $att = falcon_seo_sideload($it['image_url']); if ($att) set_post_thumbnail($id, $att); }
        $created[] = $id;
    }
    falcon_seo_log(0, 'wc_bulk_create', array('count' => count($created)), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => count($created), 'product_ids' => $created));
}
function falcon_seo_rest_wc_export(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    list($dir, $url) = falcon_seo_backup_dir();
    $products = wc_get_products(array('limit' => -1));
    $rows = array(); foreach ($products as $p) $rows[] = falcon_seo_product_dto($p);
    $name = 'products-' . gmdate('Ymd-His') . '.csv';
    $fh = fopen($dir . '/' . $name, 'w');
    fputcsv($fh, array('id', 'name', 'sku', 'type', 'regular_price', 'sale_price', 'stock_status', 'stock_qty', 'status', 'url'));
    foreach ($rows as $r) fputcsv($fh, array($r['id'], $r['name'], $r['sku'], $r['type'], $r['regular_price'], $r['sale_price'], $r['stock_status'], $r['stock_qty'], $r['status'], $r['url']));
    fclose($fh);
    return rest_ensure_response(array('exported' => count($rows), 'file' => $name, 'url' => $url . '/' . $name));
}
function falcon_seo_rest_wc_import(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $rows = array();
    if (!empty($b['csv_url'])) {
        $resp = wp_remote_get(esc_url_raw($b['csv_url']), array('timeout' => 20));
        if (is_wp_error($resp)) return new WP_Error('falcon_err', $resp->get_error_message(), array('status' => 400));
        $lines = preg_split('/\r\n|\r|\n/', wp_remote_retrieve_body($resp));
        $header = null;
        foreach ($lines as $ln) {
            if ($ln === '') continue;
            $cells = str_getcsv($ln);
            if ($header === null) { $header = array_map('trim', $cells); continue; }
            $rows[] = array_combine($header, array_pad($cells, count($header), ''));
        }
    } elseif (!empty($b['rows']) && is_array($b['rows'])) {
        $rows = $b['rows'];
    } else {
        return new WP_Error('falcon_empty', 'Provide csv_url or rows[].', array('status' => 400));
    }
    $created = 0; $updated = 0;
    foreach ($rows as $r) {
        $name = isset($r['name']) ? sanitize_text_field($r['name']) : '';
        $sku = isset($r['sku']) ? sanitize_text_field($r['sku']) : '';
        $p = null;
        if ($sku !== '') { $existing = wc_get_product_id_by_sku($sku); if ($existing) $p = wc_get_product($existing); }
        $is_new = !$p;
        if (!$p) { if ($name === '') continue; $p = new WC_Product_Simple(); $p->set_name($name); if ($sku !== '') $p->set_sku($sku); }
        elseif ($name !== '') $p->set_name($name);
        if (isset($r['regular_price']) && $r['regular_price'] !== '') $p->set_regular_price((string) $r['regular_price']);
        if (isset($r['sale_price']) && $r['sale_price'] !== '') $p->set_sale_price((string) $r['sale_price']);
        if (isset($r['stock_quantity']) && $r['stock_quantity'] !== '') { $p->set_manage_stock(true); $p->set_stock_quantity((int) $r['stock_quantity']); }
        if (isset($r['stock_status']) && $r['stock_status'] !== '') $p->set_stock_status(sanitize_key($r['stock_status']));
        $p->save();
        if ($is_new) $created++; else $updated++;
    }
    falcon_seo_log(0, 'wc_import', array('created' => $created, 'updated' => $updated), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => $created, 'updated' => $updated, 'rows' => count($rows)));
}
function falcon_seo_rest_wc_bulk_order_status(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $ids = (!empty($b['order_ids']) && is_array($b['order_ids'])) ? array_map('intval', $b['order_ids']) : array();
    $status = isset($b['status']) ? sanitize_key($b['status']) : '';
    if (!$ids || !$status) return new WP_Error('falcon_empty', 'order_ids[] and status are required.', array('status' => 400));
    $done = 0;
    foreach ($ids as $oid) { $o = wc_get_order($oid); if (!$o) continue; $o->update_status($status, isset($b['note']) ? sanitize_text_field($b['note']) : ''); $done++; }
    falcon_seo_log(0, 'wc_bulk_order_status', array('count' => $done, 'status' => $status), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => $done, 'status' => $status));
}
function falcon_seo_rest_wc_list_coupons() {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $q = new WP_Query(array('post_type' => 'shop_coupon', 'posts_per_page' => 100, 'post_status' => 'publish'));
    $rows = array();
    foreach ($q->posts as $p) {
        $c = new WC_Coupon($p->ID);
        $rows[] = array('id' => $p->ID, 'code' => $c->get_code(), 'discount_type' => $c->get_discount_type(),
            'amount' => $c->get_amount(), 'expires' => $c->get_date_expires() ? $c->get_date_expires()->date('Y-m-d') : null,
            'usage' => $c->get_usage_count());
    }
    return rest_ensure_response(array('count' => count($rows), 'coupons' => $rows));
}
function falcon_seo_rest_wc_create_coupon(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $code = isset($b['code']) ? sanitize_text_field($b['code']) : '';
    if ($code === '') return new WP_Error('falcon_empty', 'code is required.', array('status' => 400));
    $c = new WC_Coupon();
    $c->set_code($code);
    $c->set_discount_type(in_array(($b['discount_type'] ?? 'percent'), array('percent', 'fixed_cart', 'fixed_product'), true) ? $b['discount_type'] : 'percent');
    if (isset($b['amount'])) $c->set_amount((string) $b['amount']);
    if (!empty($b['expires'])) $c->set_date_expires(sanitize_text_field($b['expires']));
    if (isset($b['minimum_amount'])) $c->set_minimum_amount((string) $b['minimum_amount']);
    if (isset($b['usage_limit'])) $c->set_usage_limit((int) $b['usage_limit']);
    if (isset($b['free_shipping'])) $c->set_free_shipping((bool) $b['free_shipping']);
    $id = $c->save();
    falcon_seo_log($id, 'wc_create_coupon', array('code' => $code), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'coupon_id' => $id, 'code' => $code));
}
function falcon_seo_rest_wc_delete_coupon(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $id = (int) ($b['coupon_id'] ?? 0);
    if (!$id) return new WP_Error('falcon_empty', 'coupon_id is required.', array('status' => 400));
    $r = wp_delete_post($id, true);
    falcon_seo_log(0, 'wc_delete_coupon', array('coupon_id' => $id), '');
    return rest_ensure_response(array('deleted' => (bool) $r, 'coupon_id' => $id));
}
function falcon_seo_rest_wc_list_categories() {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $terms = get_terms(array('taxonomy' => 'product_cat', 'hide_empty' => false));
    $rows = array();
    if (!is_wp_error($terms)) foreach ($terms as $t) $rows[] = array('id' => $t->term_id, 'name' => $t->name, 'slug' => $t->slug, 'count' => $t->count, 'parent' => $t->parent);
    return rest_ensure_response(array('count' => count($rows), 'categories' => $rows));
}
function falcon_seo_rest_wc_create_category(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $b = falcon_seo_body($req);
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if ($name === '') return new WP_Error('falcon_empty', 'name is required.', array('status' => 400));
    $args = array();
    if (!empty($b['parent'])) $args['parent'] = (int) $b['parent'];
    if (!empty($b['description'])) $args['description'] = sanitize_text_field($b['description']);
    $t = wp_insert_term($name, 'product_cat', $args);
    if (is_wp_error($t)) return new WP_Error('falcon_err', $t->get_error_message(), array('status' => 400));
    falcon_seo_log(0, 'wc_create_category', array('name' => $name), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'id' => (int) $t['term_id'], 'name' => $name));
}
function falcon_seo_rest_wc_low_stock(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $threshold = $req->get_param('threshold') ? (int) $req->get_param('threshold') : 5;
    $products = wc_get_products(array('limit' => -1, 'manage_stock' => true));
    $rows = array();
    foreach ($products as $p) {
        $qty = $p->get_stock_quantity();
        if ($qty !== null && $qty <= $threshold) $rows[] = array('id' => $p->get_id(), 'name' => $p->get_name(), 'sku' => $p->get_sku(), 'stock' => $qty, 'status' => $p->get_stock_status());
    }
    usort($rows, function ($a, $b) { return $a['stock'] - $b['stock']; });
    return rest_ensure_response(array('threshold' => $threshold, 'count' => count($rows), 'products' => $rows));
}
function falcon_seo_rest_wc_top_sellers(WP_REST_Request $req) {
    $g = falcon_seo_woo_guard(); if (is_wp_error($g)) return $g;
    $limit = $req->get_param('limit') ? min(50, (int) $req->get_param('limit')) : 10;
    $q = new WP_Query(array('post_type' => 'product', 'posts_per_page' => $limit, 'meta_key' => 'total_sales',
        'orderby' => 'meta_value_num', 'order' => 'DESC', 'post_status' => 'publish'));
    $rows = array();
    foreach ($q->posts as $p) {
        $prod = wc_get_product($p->ID);
        if ($prod) $rows[] = array('id' => $p->ID, 'name' => $prod->get_name(), 'sku' => $prod->get_sku(),
            'total_sales' => (int) get_post_meta($p->ID, 'total_sales', true), 'price' => $prod->get_price());
    }
    return rest_ensure_response(array('count' => count($rows), 'top_sellers' => $rows));
}

/* ============================================================
 * Images — capabilities / webp / optimize / resize / regenerate / lazy-load
 * ============================================================ */
function falcon_seo_webp_supported() {
    if (function_exists('imagewebp')) return true;
    if (class_exists('Imagick')) { try { return (bool) count(Imagick::queryFormats('WEBP')); } catch (\Throwable $e) {} }
    return false;
}
function falcon_seo_image_targets($b) {
    if (!empty($b['media_id'])) return array((int) $b['media_id']);
    $q = new WP_Query(array('post_type' => 'attachment', 'post_mime_type' => array('image/jpeg', 'image/png'),
        'post_status' => 'inherit', 'posts_per_page' => isset($b['limit']) ? max(1, min(200, (int) $b['limit'])) : 50, 'fields' => 'ids'));
    return $q->posts;
}
function falcon_seo_rest_image_caps() {
    return rest_ensure_response(array(
        'gd' => extension_loaded('gd'), 'imagick' => extension_loaded('imagick'),
        'webp' => falcon_seo_webp_supported(),
        'native_lazyload' => function_exists('wp_lazy_loading_enabled'),
        'lazyload_setting' => get_option('falcon_seo_lazyload', null),
    ));
}
function falcon_seo_rest_convert_webp(WP_REST_Request $req) {
    if (!falcon_seo_webp_supported()) return new WP_Error('falcon_err', 'This server cannot create WebP (GD/Imagick lacks WebP support).', array('status' => 400));
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $b = falcon_seo_body($req);
    $quality = isset($b['quality']) ? max(1, min(100, (int) $b['quality'])) : 82;
    $replace = !empty($b['replace']);
    $done = array();
    foreach (falcon_seo_image_targets($b) as $id) {
        $file = get_attached_file($id);
        if (!$file || !file_exists($file)) continue;
        $editor = wp_get_image_editor($file);
        if (is_wp_error($editor)) continue;
        if (method_exists($editor, 'set_quality')) $editor->set_quality($quality);
        $webp = preg_replace('/\.(jpe?g|png)$/i', '.webp', $file);
        $saved = $editor->save($webp, 'image/webp');
        if (is_wp_error($saved)) continue;
        $path = is_array($saved) && isset($saved['path']) ? $saved['path'] : $webp;
        $entry = array('media_id' => $id, 'webp' => basename($path));
        if ($replace) {
            update_attached_file($id, $path);
            wp_update_post(array('ID' => $id, 'post_mime_type' => 'image/webp'));
            wp_update_attachment_metadata($id, wp_generate_attachment_metadata($id, $path));
            $entry['replaced'] = true;
        }
        $done[] = $entry;
    }
    falcon_seo_log(0, 'convert_webp', array('count' => count($done), 'replace' => $replace), falcon_seo_reason($b));
    return rest_ensure_response(array('converted' => count($done), 'results' => $done));
}
function falcon_seo_rest_optimize_images(WP_REST_Request $req) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $b = falcon_seo_body($req);
    $quality = isset($b['quality']) ? max(1, min(100, (int) $b['quality'])) : 75;
    $done = array();
    foreach (falcon_seo_image_targets($b) as $id) {
        $file = get_attached_file($id);
        if (!$file || !file_exists($file)) continue;
        $before = filesize($file);
        $editor = wp_get_image_editor($file);
        if (is_wp_error($editor)) continue;
        if (method_exists($editor, 'set_quality')) $editor->set_quality($quality);
        $saved = $editor->save($file);
        if (is_wp_error($saved)) continue;
        clearstatcache(true, $file);
        $done[] = array('media_id' => $id, 'before' => $before, 'after' => filesize($file));
    }
    if ($done) { foreach ($done as $d) wp_update_attachment_metadata($d['media_id'], wp_generate_attachment_metadata($d['media_id'], get_attached_file($d['media_id']))); }
    falcon_seo_log(0, 'optimize_images', array('count' => count($done), 'quality' => $quality), falcon_seo_reason($b));
    return rest_ensure_response(array('optimized' => count($done), 'results' => $done));
}
function falcon_seo_rest_resize_image(WP_REST_Request $req) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $b = falcon_seo_body($req);
    $id = (int) ($b['media_id'] ?? 0);
    if (!$id) return new WP_Error('falcon_empty', 'media_id is required.', array('status' => 400));
    $w = isset($b['max_width']) ? (int) $b['max_width'] : null;
    $h = isset($b['max_height']) ? (int) $b['max_height'] : null;
    if (!$w && !$h) return new WP_Error('falcon_empty', 'max_width and/or max_height is required.', array('status' => 400));
    $file = get_attached_file($id);
    if (!$file || !file_exists($file)) return new WP_Error('falcon_not_found', 'Image file not found.', array('status' => 404));
    $editor = wp_get_image_editor($file);
    if (is_wp_error($editor)) return new WP_Error('falcon_err', $editor->get_error_message(), array('status' => 400));
    $editor->resize($w, $h, !empty($b['crop']));
    $saved = $editor->save($file);
    if (is_wp_error($saved)) return new WP_Error('falcon_err', $saved->get_error_message(), array('status' => 400));
    wp_update_attachment_metadata($id, wp_generate_attachment_metadata($id, $file));
    falcon_seo_log($id, 'resize_image', array('w' => $w, 'h' => $h), falcon_seo_reason($b));
    return rest_ensure_response(array('resized' => true, 'media_id' => $id, 'size' => $editor->get_size()));
}
function falcon_seo_rest_regen_thumbs(WP_REST_Request $req) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $b = falcon_seo_body($req);
    $ids = !empty($b['media_id']) ? array((int) $b['media_id']) : falcon_seo_image_targets($b);
    $done = 0;
    foreach ($ids as $id) {
        $file = get_attached_file($id);
        if (!$file || !file_exists($file)) continue;
        wp_update_attachment_metadata($id, wp_generate_attachment_metadata($id, $file));
        $done++;
    }
    falcon_seo_log(0, 'regenerate_thumbnails', array('count' => $done), falcon_seo_reason($b));
    return rest_ensure_response(array('regenerated' => $done));
}
function falcon_seo_rest_lazyload(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $enabled = !isset($b['enabled']) || !empty($b['enabled']);
    update_option('falcon_seo_lazyload', $enabled ? 1 : 0);
    falcon_seo_log(0, 'lazyload', array('enabled' => $enabled), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'lazyload_enabled' => $enabled));
}

/* ============================================================
 * SEO power — internal links / content audit / 404 log / schema templates / hreflang
 * ============================================================ */
function falcon_seo_rest_internal_links(WP_REST_Request $req) {
    $host = wp_parse_url(home_url(), PHP_URL_HOST);
    $front = (int) get_option('page_on_front');
    $q = new WP_Query(array('post_type' => array('post', 'page'), 'post_status' => 'publish', 'posts_per_page' => -1, 'fields' => 'ids'));
    $ids = $q->posts;
    $inbound = array_fill_keys($ids, 0);
    $outbound = array();
    foreach ($ids as $pid) {
        $content = get_post_field('post_content', $pid);
        $out = 0;
        if (preg_match_all('/href=["\']([^"\']+)["\']/i', $content, $m)) {
            foreach (array_unique($m[1]) as $href) {
                if (strpos($href, $host) === false && strpos($href, 'http') === 0) continue; // external
                $target = url_to_postid($href);
                if ($target && isset($inbound[$target])) { $inbound[$target]++; $out++; }
            }
        }
        $outbound[$pid] = $out;
    }
    $orphans = array();
    foreach ($inbound as $pid => $cnt) {
        if ($cnt === 0 && $pid !== $front) $orphans[] = array('id' => $pid, 'title' => get_the_title($pid), 'url' => get_permalink($pid), 'outbound' => $outbound[$pid] ?? 0);
    }
    arsort($inbound);
    $most = array();
    foreach (array_slice($inbound, 0, 10, true) as $pid => $cnt) $most[] = array('id' => $pid, 'title' => get_the_title($pid), 'inbound' => $cnt);
    return rest_ensure_response(array('analyzed' => count($ids), 'orphan_count' => count($orphans), 'orphans' => $orphans, 'most_linked' => $most));
}
function falcon_seo_rest_content_audit(WP_REST_Request $req) {
    $limit = $req->get_param('limit') ? min(1000, (int) $req->get_param('limit')) : 500;
    $type = $req->get_param('type') ? sanitize_key($req->get_param('type')) : 'post';
    $q = new WP_Query(array('post_type' => $type, 'post_status' => 'publish', 'posts_per_page' => $limit));
    $thin = array(); $no_meta = array(); $no_focus = array(); $no_image = array(); $titles = array(); $dupes = array();
    foreach ($q->posts as $p) {
        $words = str_word_count(wp_strip_all_tags($p->post_content));
        if ($words < 300) $thin[] = array('id' => $p->ID, 'title' => get_the_title($p), 'words' => $words);
        $meta = falcon_seo_get_meta($p->ID);
        if ($meta['meta_description'] === '') $no_meta[] = array('id' => $p->ID, 'title' => get_the_title($p));
        if ($meta['focus_keyword'] === '') $no_focus[] = array('id' => $p->ID, 'title' => get_the_title($p));
        if (!has_post_thumbnail($p->ID)) $no_image[] = array('id' => $p->ID, 'title' => get_the_title($p));
        $t = strtolower($meta['seo_title'] !== '' ? $meta['seo_title'] : get_the_title($p));
        if (isset($titles[$t])) $dupes[] = array('title' => $t, 'ids' => array($titles[$t], $p->ID));
        else $titles[$t] = $p->ID;
    }
    return rest_ensure_response(array(
        'analyzed' => $q->post_count, 'post_type' => $type,
        'thin_content' => $thin, 'missing_meta_description' => $no_meta,
        'missing_focus_keyword' => $no_focus, 'missing_featured_image' => $no_image,
        'duplicate_titles' => $dupes,
        'summary' => array('thin' => count($thin), 'no_meta' => count($no_meta), 'no_focus' => count($no_focus), 'no_image' => count($no_image), 'duplicate_titles' => count($dupes)),
    ));
}
function falcon_seo_rest_get_404log() {
    $log = array_values(get_option('falcon_seo_404log', array()));
    usort($log, function ($a, $b) { return $b['count'] - $a['count']; });
    return rest_ensure_response(array('count' => count($log), 'log' => $log));
}
function falcon_seo_rest_clear_404log() {
    delete_option('falcon_seo_404log');
    return rest_ensure_response(array('cleared' => true));
}
function falcon_seo_rest_schema_template(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $post = get_post($id);
    if (!$post) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $tpl = isset($b['template']) ? sanitize_text_field($b['template']) : '';
    $data = isset($b['data']) && is_array($b['data']) ? $b['data'] : array();
    $schema = array('@context' => 'https://schema.org');
    switch ($tpl) {
        case 'Article':
        case 'BlogPosting':
            $schema['@type'] = $tpl;
            $schema['headline'] = get_the_title($post);
            $schema['datePublished'] = get_the_date('c', $post);
            $schema['dateModified'] = get_the_modified_date('c', $post);
            $schema['author'] = array('@type' => 'Person', 'name' => get_the_author_meta('display_name', $post->post_author));
            $schema['mainEntityOfPage'] = get_permalink($post);
            if (has_post_thumbnail($id)) $schema['image'] = get_the_post_thumbnail_url($id, 'full');
            break;
        case 'Product':
            $schema['@type'] = 'Product';
            $schema['name'] = get_the_title($post);
            if (!empty($data['price'])) $schema['offers'] = array('@type' => 'Offer', 'price' => (string) $data['price'], 'priceCurrency' => $data['currency'] ?? 'USD', 'availability' => 'https://schema.org/InStock');
            if (has_post_thumbnail($id)) $schema['image'] = get_the_post_thumbnail_url($id, 'full');
            break;
        case 'FAQPage':
        case 'FAQ':
            $schema['@type'] = 'FAQPage';
            $schema['mainEntity'] = array();
            foreach (($data['questions'] ?? array()) as $qa) {
                $schema['mainEntity'][] = array('@type' => 'Question', 'name' => $qa['question'] ?? '',
                    'acceptedAnswer' => array('@type' => 'Answer', 'text' => $qa['answer'] ?? ''));
            }
            break;
        case 'LocalBusiness':
            $schema['@type'] = 'LocalBusiness';
            $schema['name'] = $data['name'] ?? get_bloginfo('name');
            if (!empty($data['telephone'])) $schema['telephone'] = $data['telephone'];
            if (!empty($data['address'])) $schema['address'] = $data['address'];
            break;
        case 'BreadcrumbList':
            $schema['@type'] = 'BreadcrumbList';
            $schema['itemListElement'] = array();
            $pos = 1;
            foreach (($data['items'] ?? array()) as $crumb) {
                $schema['itemListElement'][] = array('@type' => 'ListItem', 'position' => $pos++, 'name' => $crumb['name'] ?? '', 'item' => $crumb['url'] ?? '');
            }
            break;
        default:
            return new WP_Error('falcon_err', 'Unknown template. Use Article, Product, FAQPage, LocalBusiness or BreadcrumbList.', array('status' => 400));
    }
    $json = wp_json_encode($schema, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    update_post_meta($id, '_falcon_schema_jsonld', wp_slash($json));
    falcon_seo_log($id, 'schema_template', array('template' => $tpl), falcon_seo_reason($b));
    return rest_ensure_response(array('applied' => true, 'post_id' => $id, 'template' => $tpl, 'schema' => $schema));
}
function falcon_seo_rest_get_hreflang(WP_REST_Request $req) {
    $id = (int) $req['id'];
    $alts = get_post_meta($id, '_falcon_hreflang', true);
    return rest_ensure_response(array('post_id' => $id, 'hreflang' => is_array($alts) ? $alts : array()));
}
function falcon_seo_rest_set_hreflang(WP_REST_Request $req) {
    $id = (int) $req['id'];
    if (!get_post($id)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $alts = isset($b['alternates']) && is_array($b['alternates']) ? $b['alternates'] : null;
    if ($alts === null) return new WP_Error('falcon_empty', 'alternates:[{lang, url}] is required (empty array removes them).', array('status' => 400));
    $clean = array();
    foreach ($alts as $a) {
        if (empty($a['lang']) || empty($a['url'])) continue;
        $clean[] = array('lang' => sanitize_text_field($a['lang']), 'url' => esc_url_raw($a['url']));
    }
    if ($clean) update_post_meta($id, '_falcon_hreflang', $clean); else delete_post_meta($id, '_falcon_hreflang');
    falcon_seo_log($id, 'set_hreflang', array('count' => count($clean)), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $id, 'hreflang' => $clean));
}

/* ============================================================
 * v1.6 — content ops
 * ============================================================ */
function falcon_seo_rest_stale_content(WP_REST_Request $req) {
    $days = $req->get_param('days') ? (int) $req->get_param('days') : 180;
    $limit = $req->get_param('limit') ? min(100, (int) $req->get_param('limit')) : 20;
    $type = $req->get_param('type') ? sanitize_key($req->get_param('type')) : 'post';
    $before = gmdate('Y-m-d H:i:s', time() - $days * 86400);
    $q = new WP_Query(array('post_type' => $type, 'post_status' => 'publish', 'posts_per_page' => $limit,
        'orderby' => 'modified', 'order' => 'ASC', 'date_query' => array(array('column' => 'post_modified_gmt', 'before' => $before))));
    $rows = array();
    foreach ($q->posts as $p) {
        $rows[] = array('id' => $p->ID, 'title' => get_the_title($p), 'url' => get_permalink($p),
            'modified' => $p->post_modified_gmt, 'words' => str_word_count(wp_strip_all_tags($p->post_content)));
    }
    return rest_ensure_response(array('older_than_days' => $days, 'count' => count($rows), 'stale' => $rows));
}
function falcon_seo_rest_restore_revision(WP_REST_Request $req) {
    $pid = (int) $req['id'];
    $b = falcon_seo_body($req);
    $rev = (int) ($b['revision_id'] ?? 0);
    if (!$rev) return new WP_Error('falcon_empty', 'revision_id is required (see get_post_revisions).', array('status' => 400));
    $r = wp_restore_post_revision($rev);
    if (!$r) return new WP_Error('falcon_err', 'Could not restore that revision.', array('status' => 400));
    falcon_seo_log($pid, 'restore_revision', array('revision_id' => $rev), falcon_seo_reason($b));
    return rest_ensure_response(array('restored' => true, 'post_id' => $pid, 'revision_id' => $rev));
}
function falcon_seo_rest_list_blocks() {
    $q = new WP_Query(array('post_type' => 'wp_block', 'posts_per_page' => 100, 'post_status' => 'publish'));
    $rows = array();
    foreach ($q->posts as $p) $rows[] = array('id' => $p->ID, 'title' => $p->post_title);
    return rest_ensure_response(array('count' => count($rows), 'reusable_blocks' => $rows));
}
function falcon_seo_rest_create_block(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $title = isset($b['title']) ? sanitize_text_field($b['title']) : '';
    if ($title === '' || !isset($b['content'])) return new WP_Error('falcon_empty', 'title and content are required.', array('status' => 400));
    $id = wp_insert_post(array('post_type' => 'wp_block', 'post_status' => 'publish', 'post_title' => $title, 'post_content' => $b['content']), true);
    if (is_wp_error($id)) return new WP_Error('falcon_err', $id->get_error_message(), array('status' => 400));
    falcon_seo_log($id, 'create_reusable_block', array('title' => $title), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'block_id' => $id));
}
function falcon_seo_rest_list_taxonomies() {
    $rows = array();
    foreach (get_taxonomies(array('public' => true), 'objects') as $tx) {
        $rows[] = array('slug' => $tx->name, 'label' => $tx->label, 'hierarchical' => $tx->hierarchical,
            'post_types' => $tx->object_type);
    }
    return rest_ensure_response(array('count' => count($rows), 'taxonomies' => $rows));
}
function falcon_seo_rest_create_term(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $tax = isset($b['taxonomy']) ? sanitize_key($b['taxonomy']) : '';
    $name = isset($b['name']) ? sanitize_text_field($b['name']) : '';
    if (!$tax || !taxonomy_exists($tax)) return new WP_Error('falcon_err', 'Valid taxonomy is required (see list_taxonomies).', array('status' => 400));
    if ($name === '') return new WP_Error('falcon_empty', 'name is required.', array('status' => 400));
    $args = array();
    if (!empty($b['parent'])) $args['parent'] = (int) $b['parent'];
    if (!empty($b['description'])) $args['description'] = sanitize_text_field($b['description']);
    $t = wp_insert_term($name, $tax, $args);
    if (is_wp_error($t)) return new WP_Error('falcon_err', $t->get_error_message(), array('status' => 400));
    falcon_seo_log(0, 'create_term', array('taxonomy' => $tax, 'name' => $name), falcon_seo_reason($b));
    return rest_ensure_response(array('created' => true, 'term_id' => (int) $t['term_id'], 'taxonomy' => $tax));
}
function falcon_seo_rest_assign_terms(WP_REST_Request $req) {
    $pid = (int) $req['id'];
    if (!get_post($pid)) return new WP_Error('falcon_not_found', 'Post not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $tax = isset($b['taxonomy']) ? sanitize_key($b['taxonomy']) : '';
    if (!$tax || !taxonomy_exists($tax)) return new WP_Error('falcon_err', 'Valid taxonomy is required.', array('status' => 400));
    $terms = $b['terms'] ?? null;
    if ($terms === null) return new WP_Error('falcon_empty', 'terms is required (names or ids, string or array).', array('status' => 400));
    if (!is_array($terms)) $terms = array_map('trim', explode(',', (string) $terms));
    $append = !empty($b['append']);
    $r = wp_set_object_terms($pid, $terms, $tax, $append);
    if (is_wp_error($r)) return new WP_Error('falcon_err', $r->get_error_message(), array('status' => 400));
    falcon_seo_log($pid, 'assign_terms', array('taxonomy' => $tax, 'terms' => $terms), falcon_seo_reason($b));
    return rest_ensure_response(array('updated' => true, 'post_id' => $pid, 'taxonomy' => $tax, 'term_ids' => $r));
}

/* ============================================================
 * v1.6 — site health & accessibility
 * ============================================================ */
function falcon_seo_rest_site_health() {
    global $wpdb;
    falcon_seo_load_upgrader();
    $core = get_core_updates();
    return rest_ensure_response(array(
        'wp_version' => get_bloginfo('version'),
        'php_version' => PHP_VERSION,
        'mysql_version' => $wpdb->db_version(),
        'https' => is_ssl() || strpos(get_option('siteurl'), 'https://') === 0,
        'debug_mode' => (defined('WP_DEBUG') && WP_DEBUG),
        'memory_limit' => defined('WP_MEMORY_LIMIT') ? WP_MEMORY_LIMIT : ini_get('memory_limit'),
        'object_cache' => wp_using_ext_object_cache(),
        'cron_disabled' => (defined('DISABLE_WP_CRON') && DISABLE_WP_CRON),
        'core_update_available' => (!empty($core) && isset($core[0]->response) && $core[0]->response === 'upgrade') ? $core[0]->current : null,
        'plugins_outdated' => count(get_plugin_updates()),
        'themes_outdated' => count(get_theme_updates()),
        'active_plugins' => count((array) get_option('active_plugins', array())),
        'multisite' => is_multisite(),
        'language' => get_bloginfo('language'),
    ));
}
function falcon_seo_rest_accessibility(WP_REST_Request $req) {
    $limit = $req->get_param('limit') ? min(200, (int) $req->get_param('limit')) : 50;
    $q = new WP_Query(array('post_type' => array('post', 'page'), 'post_status' => 'publish', 'posts_per_page' => $limit));
    $generic = array('click here', 'read more', 'here', 'link', 'more');
    $issues = array(); $img_no_alt = 0; $vague_links = 0;
    foreach ($q->posts as $p) {
        $c = $p->post_content;
        $row = array('id' => $p->ID, 'title' => get_the_title($p), 'problems' => array());
        if (preg_match_all('/<img[^>]*>/i', $c, $imgs)) {
            foreach ($imgs[0] as $img) {
                if (!preg_match('/\balt\s*=\s*["\'][^"\']+["\']/i', $img)) { $img_no_alt++; $row['problems'][] = 'image missing alt'; }
            }
        }
        if (preg_match_all('/<a[^>]*>(.*?)<\/a>/is', $c, $links)) {
            foreach ($links[1] as $txt) {
                $t = strtolower(trim(wp_strip_all_tags($txt)));
                if ($t !== '' && in_array($t, $generic, true)) { $vague_links++; $row['problems'][] = 'vague link text: "' . $t . '"'; }
            }
        }
        if ($row['problems']) $issues[] = $row;
    }
    return rest_ensure_response(array(
        'site_language_set' => (bool) get_bloginfo('language'),
        'images_missing_alt' => $img_no_alt, 'vague_links' => $vague_links,
        'pages_with_issues' => count($issues), 'issues' => $issues,
        'note' => 'Heuristic content scan (alt text, link text, lang). Not a full WCAG audit — use a dedicated tool for legal compliance.',
    ));
}

/* ============================================================
 * v1.6 — SMTP & spam
 * ============================================================ */
function falcon_seo_rest_configure_smtp(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    foreach (array('host', 'username', 'password') as $req_k) {
        if (empty($b[$req_k])) return new WP_Error('falcon_empty', "$req_k is required.", array('status' => 400));
    }
    $cfg = array(
        'host' => sanitize_text_field($b['host']),
        'port' => isset($b['port']) ? (int) $b['port'] : 587,
        'username' => sanitize_text_field($b['username']),
        'password' => (string) $b['password'],
        'encryption' => in_array(($b['encryption'] ?? 'tls'), array('tls', 'ssl', ''), true) ? $b['encryption'] : 'tls',
        'from_email' => isset($b['from_email']) ? sanitize_email($b['from_email']) : '',
        'from_name' => isset($b['from_name']) ? sanitize_text_field($b['from_name']) : '',
    );
    update_option('falcon_seo_smtp', $cfg);
    falcon_seo_log(0, 'configure_smtp', array('host' => $cfg['host'], 'port' => $cfg['port']), falcon_seo_reason($b));
    return rest_ensure_response(array('configured' => true, 'host' => $cfg['host'], 'port' => $cfg['port']));
}
// Applied live from the stored option on every mail send — no code is written to disk,
// so credentials never sit in a plain-text PHP file, and SMTP simply stops applying if
// this plugin is ever deactivated (rather than lingering as an orphaned mu-plugin).
add_action('phpmailer_init', function ($m) {
    $c = get_option('falcon_seo_smtp');
    if (!is_array($c) || empty($c['host']) || empty($c['username'])) return;
    $m->isSMTP();
    $m->Host = $c['host'];
    $m->Port = (int) ($c['port'] ?? 587);
    $m->SMTPAuth = true;
    $m->Username = $c['username'];
    $m->Password = (string) ($c['password'] ?? '');
    if (!empty($c['encryption'])) $m->SMTPSecure = $c['encryption'];
});
add_filter('wp_mail_from', function ($email) {
    $c = get_option('falcon_seo_smtp');
    return (!empty($c['from_email'])) ? $c['from_email'] : $email;
});
add_filter('wp_mail_from_name', function ($name) {
    $c = get_option('falcon_seo_smtp');
    return (!empty($c['from_name'])) ? $c['from_name'] : $name;
});
function falcon_seo_rest_test_email(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $to = isset($b['to']) ? sanitize_email($b['to']) : get_option('admin_email');
    if (!$to) return new WP_Error('falcon_empty', 'A valid `to` email is required.', array('status' => 400));
    $ok = wp_mail($to, 'SMTP test', 'This is a test email from the TechShu SEO Bridge plugin. If you received it, email sending works.');
    return rest_ensure_response(array('sent' => (bool) $ok, 'to' => $to,
        'note' => $ok ? 'Sent — check the inbox (and spam).' : 'wp_mail returned false. Check SMTP settings/credentials.'));
}
function falcon_seo_rest_purge_spam() {
    global $wpdb;
    $n = (int) $wpdb->query("DELETE FROM {$wpdb->comments} WHERE comment_approved='spam'");
    falcon_seo_log(0, 'purge_spam', array('deleted' => $n), '');
    return rest_ensure_response(array('deleted' => $n));
}
function falcon_seo_rest_export_wxr() {
    require_once ABSPATH . 'wp-admin/includes/export.php';
    list($dir, $url) = falcon_seo_backup_dir();
    $name = 'export-' . gmdate('Ymd-His') . '.xml';
    ob_start();
    export_wp(array('content' => 'all'));
    $xml = ob_get_clean();
    file_put_contents($dir . '/' . $name, $xml);
    return rest_ensure_response(array('exported' => true, 'file' => $name, 'url' => $url . '/' . $name, 'bytes' => strlen($xml)));
}

/* ============================================================
 * v1.6 — WooCommerce reviews & refunds
 * ============================================================ */
function falcon_seo_rest_wc_reviews(WP_REST_Request $req) {
    if (!class_exists('WooCommerce')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $status = $req->get_param('status') ? sanitize_key($req->get_param('status')) : 'all';
    $map = array('pending' => 'hold', 'approved' => 'approve', 'spam' => 'spam', 'all' => 'all');
    $comments = get_comments(array('type' => 'review', 'status' => $map[$status] ?? 'all',
        'number' => $req->get_param('per_page') ? (int) $req->get_param('per_page') : 30));
    $rows = array();
    foreach ($comments as $c) {
        $rows[] = array('id' => $c->comment_ID, 'product_id' => (int) $c->comment_post_ID, 'product' => get_the_title($c->comment_post_ID),
            'author' => $c->comment_author, 'rating' => (int) get_comment_meta($c->comment_ID, 'rating', true),
            'content' => wp_trim_words($c->comment_content, 40), 'status' => wp_get_comment_status($c->comment_ID), 'date' => $c->comment_date_gmt);
    }
    return rest_ensure_response(array('count' => count($rows), 'reviews' => $rows));
}
function falcon_seo_rest_wc_moderate_review(WP_REST_Request $req) {
    if (!class_exists('WooCommerce')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $id = (int) $req['id'];
    $b = falcon_seo_body($req);
    $status = isset($b['status']) ? sanitize_key($b['status']) : '';
    $map = array('approve' => 'approve', 'approved' => 'approve', 'hold' => 'hold', 'pending' => 'hold', 'spam' => 'spam', 'trash' => 'trash');
    if (!isset($map[$status])) return new WP_Error('falcon_err', 'status must be approve|hold|spam|trash.', array('status' => 400));
    $r = wp_set_comment_status($id, $map[$status]);
    falcon_seo_log(0, 'wc_moderate_review', array('review_id' => $id, 'status' => $map[$status]), '');
    return rest_ensure_response(array('updated' => (bool) $r, 'review_id' => $id, 'status' => $map[$status]));
}
function falcon_seo_rest_wc_refund(WP_REST_Request $req) {
    if (!class_exists('WooCommerce')) return new WP_Error('falcon_woo', 'WooCommerce is not active.', array('status' => 400));
    $id = (int) $req['id'];
    $order = wc_get_order($id);
    if (!$order) return new WP_Error('falcon_not_found', 'Order not found.', array('status' => 404));
    $b = falcon_seo_body($req);
    $amount = isset($b['amount']) ? (float) $b['amount'] : (float) $order->get_remaining_refund_amount();
    if ($amount <= 0) return new WP_Error('falcon_err', 'Nothing left to refund (or amount must be > 0).', array('status' => 400));
    $refund = wc_create_refund(array(
        'order_id' => $id, 'amount' => $amount,
        'reason' => isset($b['reason']) ? sanitize_text_field($b['reason']) : '',
        'refund_payment' => !empty($b['refund_payment']),
        'restock_items' => !empty($b['restock']),
    ));
    if (is_wp_error($refund)) return new WP_Error('falcon_err', $refund->get_error_message(), array('status' => 400));
    falcon_seo_log($id, 'wc_refund', array('amount' => $amount), falcon_seo_reason($b));
    return rest_ensure_response(array('refunded' => true, 'order_id' => $id, 'amount' => $amount, 'refund_id' => $refund->get_id()));
}

/* ============================================================
 * v1.6 — Google Indexing API
 * ============================================================ */
function falcon_seo_rest_set_google_sa(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $json = $b['service_account_json'] ?? null;
    if (is_string($json)) $json = json_decode($json, true);
    if (!is_array($json) || empty($json['client_email']) || empty($json['private_key'])) {
        return new WP_Error('falcon_err', 'service_account_json must be the full service-account JSON (with client_email + private_key).', array('status' => 400));
    }
    update_option('falcon_seo_google_sa', $json);
    return rest_ensure_response(array('saved' => true, 'client_email' => $json['client_email']));
}
function falcon_seo_google_token() {
    $sa = get_option('falcon_seo_google_sa');
    if (!is_array($sa) || empty($sa['client_email']) || empty($sa['private_key'])) return null;
    $b64 = function ($d) { return rtrim(strtr(base64_encode($d), '+/', '-_'), '='); };
    $now = time();
    $aud = $sa['token_uri'] ?? 'https://oauth2.googleapis.com/token';
    $header = $b64(wp_json_encode(array('alg' => 'RS256', 'typ' => 'JWT')));
    $claim = $b64(wp_json_encode(array('iss' => $sa['client_email'], 'scope' => 'https://www.googleapis.com/auth/indexing',
        'aud' => $aud, 'iat' => $now, 'exp' => $now + 3600)));
    $sig = '';
    if (!openssl_sign($header . '.' . $claim, $sig, $sa['private_key'], 'sha256WithRSAEncryption')) return null;
    $jwt = $header . '.' . $claim . '.' . $b64($sig);
    $resp = wp_remote_post($aud, array('timeout' => 15, 'body' => array(
        'grant_type' => 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion' => $jwt)));
    if (is_wp_error($resp)) return null;
    $body = json_decode(wp_remote_retrieve_body($resp), true);
    return $body['access_token'] ?? null;
}
function falcon_seo_rest_index_url(WP_REST_Request $req) {
    $b = falcon_seo_body($req);
    $url = isset($b['url']) ? esc_url_raw($b['url']) : '';
    if (!$url) return new WP_Error('falcon_empty', 'url is required.', array('status' => 400));
    $type = ($b['type'] ?? 'URL_UPDATED') === 'URL_DELETED' ? 'URL_DELETED' : 'URL_UPDATED';
    $token = falcon_seo_google_token();
    if (!$token) return new WP_Error('falcon_err', 'No Google token. Set the service-account JSON first (set_google_service_account) and ensure the Indexing API is enabled + the SA is an owner in Search Console.', array('status' => 400));
    $resp = wp_remote_post('https://indexing.googleapis.com/v3/urlNotifications:publish', array(
        'timeout' => 15, 'headers' => array('Authorization' => 'Bearer ' . $token, 'Content-Type' => 'application/json'),
        'body' => wp_json_encode(array('url' => $url, 'type' => $type))));
    if (is_wp_error($resp)) return new WP_Error('falcon_err', $resp->get_error_message(), array('status' => 400));
    $code = wp_remote_retrieve_response_code($resp);
    $body = json_decode(wp_remote_retrieve_body($resp), true);
    falcon_seo_log(0, 'index_url', array('url' => $url, 'type' => $type, 'http' => $code), falcon_seo_reason($b));
    return rest_ensure_response(array('submitted' => $code >= 200 && $code < 300, 'http' => $code, 'response' => $body));
}
function falcon_seo_rest_index_status(WP_REST_Request $req) {
    $url = $req->get_param('url') ? esc_url_raw($req->get_param('url')) : '';
    if (!$url) return new WP_Error('falcon_empty', 'url is required.', array('status' => 400));
    $token = falcon_seo_google_token();
    if (!$token) return new WP_Error('falcon_err', 'No Google token. Set the service-account JSON first.', array('status' => 400));
    $resp = wp_remote_get('https://indexing.googleapis.com/v3/urlNotifications/metadata?url=' . rawurlencode($url),
        array('timeout' => 15, 'headers' => array('Authorization' => 'Bearer ' . $token)));
    if (is_wp_error($resp)) return new WP_Error('falcon_err', $resp->get_error_message(), array('status' => 400));
    return rest_ensure_response(json_decode(wp_remote_retrieve_body($resp), true) ?: array('http' => wp_remote_retrieve_response_code($resp)));
}

/* ============================================================
 * Admin UI — settings + pending review
 * ============================================================ */
add_action('admin_menu', function () {
    add_menu_page('TechShu SEO Bridge', 'TechShu SEO Bridge', 'manage_options', 'falcon-seo', 'falcon_seo_admin_page', 'dashicons-chart-line', 80);
});

add_action('admin_enqueue_scripts', function ($hook) {
    if ($hook !== 'toplevel_page_falcon-seo') return;
    wp_register_script('falcon-seo-admin', '', array(), FALCON_SEO_VERSION, true);
    wp_enqueue_script('falcon-seo-admin');
    wp_localize_script('falcon-seo-admin', 'falconSeoAdmin', array(
        'base'  => esc_url_raw(rest_url('falcon/v1')),
        'token' => get_option('falcon_seo_token'),
    ));
    wp_add_inline_script('falcon-seo-admin', falcon_seo_admin_js());
});
function falcon_seo_admin_js() {
    return <<<'JS'
(function () {
    var base = falconSeoAdmin.base;
    var token = falconSeoAdmin.token;
    var btn = document.getElementById('falcon-test-btn');
    var out = document.getElementById('falcon-test-result');
    if (!btn) return;
    btn.addEventListener('click', function (e) {
        e.preventDefault();
        out.textContent = 'Testing…'; out.style.color = '#555';
        // 1) unauthenticated self-test — does the auth header reach PHP?
        fetch(base + '/selftest').then(function (r) { return r.json(); }).then(function (self) {
            // 2) authenticated call
            return fetch(base + '/site', { headers: { 'Authorization': 'Bearer ' + token } }).then(function (r) {
                if (r.ok) {
                    out.innerHTML = '<strong style="color:#1a7f37;">✅ Connected — token valid. You\'re ready to use Falcon.</strong>';
                } else if (r.status === 401 || r.status === 403) {
                    if (self && self.auth_header_received === false) {
                        out.innerHTML = '<strong style="color:#b32d2e;">❌ Your server is stripping the Authorization header.</strong> Falcon tried to auto-fix this in .htaccess. Re-activate the plugin; if it persists, ask your host to allow the Authorization header.';
                    } else {
                        out.innerHTML = '<strong style="color:#b32d2e;">❌ Token rejected.</strong> Make sure you pasted this exact token into the Falcon portal.';
                    }
                } else {
                    out.innerHTML = '<strong style="color:#b32d2e;">❌ Unexpected response (' + r.status + ').</strong>';
                }
            });
        }).catch(function () {
            out.innerHTML = '<strong style="color:#b32d2e;">❌ Could not reach the REST API.</strong> Check that pretty permalinks are on and the site is publicly reachable.';
        });
    });
})();
JS;
}

add_action('admin_post_falcon_seo_regen', 'falcon_seo_handle_regen');
function falcon_seo_handle_regen() {
    if (!current_user_can('manage_options')) wp_die('Nope.');
    check_admin_referer('falcon_seo_regen');
    update_option('falcon_seo_token', wp_generate_password(40, false, false));
    wp_redirect(admin_url('admin.php?page=falcon-seo&regen=1'));
    exit;
}

function falcon_seo_admin_page() {
    if (!current_user_can('manage_options')) return;
    global $wpdb;
    $token = get_option('falcon_seo_token');
    $site  = get_site_url();
    $rows  = $wpdb->get_results("SELECT * FROM " . falcon_seo_table() . " WHERE status='applied' ORDER BY created_at DESC LIMIT 50", ARRAY_A);
    ?>
    <div class="wrap">
        <h1>TechShu SEO Bridge</h1>

        <?php if (!falcon_seo_yoast_active()): ?>
            <div class="notice notice-warning"><p><strong>Yoast SEO is not active.</strong> Install/activate Yoast so SEO meta fields can be written.</p></div>
        <?php endif; ?>

        <div class="notice notice-info inline"><p>Changes from Falcon are applied <strong>live, automatically</strong> (AI-controlled via Claude/ChatGPT). This screen is a read-only history.</p></div>

        <h2>Connect to Falcon</h2>
        <p>In the Falcon portal, add a <strong>WordPress (SEO)</strong> connector and paste these:</p>
        <table class="form-table">
            <tr><th>Site URL</th><td><code><?php echo esc_html($site); ?></code></td></tr>
            <tr><th>API token</th><td><code style="user-select:all"><?php echo esc_html($token); ?></code></td></tr>
        </table>
        <p>
            <button class="button button-primary" id="falcon-test-btn">Test connection</button>
            <span id="falcon-test-result" style="margin-left:10px;"></span>
        </p>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>" onsubmit="return confirm('Regenerate token? The old one stops working.');">
            <?php wp_nonce_field('falcon_seo_regen'); ?>
            <input type="hidden" name="action" value="falcon_seo_regen">
            <button class="button">Regenerate token</button>
        </form>

        <h2 style="margin-top:2em;">Recent changes (<?php echo count($rows); ?>)</h2>
        <?php if (empty($rows)): ?>
            <p>No changes yet. When Falcon updates SEO meta or links, they appear here.</p>
        <?php else: ?>
            <table class="widefat striped">
                <thead><tr><th>When</th><th>Post</th><th>Type</th><th>What changed</th><th>Reason</th></tr></thead>
                <tbody>
                <?php foreach ($rows as $r): $p = json_decode($r['payload'], true); ?>
                    <tr>
                        <td><?php echo esc_html($r['created_at']); ?></td>
                        <td><a href="<?php echo esc_url(get_permalink((int)$r['post_id'])); ?>" target="_blank"><?php echo esc_html(get_the_title((int)$r['post_id'])); ?></a></td>
                        <td><?php echo esc_html($r['change_type']); ?></td>
                        <td style="max-width:380px;">
                            <?php if ($r['change_type'] === 'seo_meta'): ?>
                                <?php if (!empty($p['title'])): ?><div><strong>Title:</strong> <?php echo esc_html($p['title']); ?></div><?php endif; ?>
                                <?php if (!empty($p['meta_description'])): ?><div><strong>Meta:</strong> <?php echo esc_html($p['meta_description']); ?></div><?php endif; ?>
                                <?php if (!empty($p['focus_keyword'])): ?><div><strong>Focus kw:</strong> <?php echo esc_html($p['focus_keyword']); ?></div><?php endif; ?>
                            <?php else: ?>
                                <div><strong><?php echo esc_html($p['anchor_text']); ?></strong> &rarr; <?php echo esc_html($p['target_url']); ?></div>
                            <?php endif; ?>
                        </td>
                        <td><?php echo esc_html($r['reason']); ?></td>
                    </tr>
                <?php endforeach; ?>
                </tbody>
            </table>
        <?php endif; ?>
    </div>
    <?php
}
