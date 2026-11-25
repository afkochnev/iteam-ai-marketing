#!/usr/bin/env bash
set -euo pipefail

fixture="iteam-wp-$(python -c 'import uuid; print(uuid.uuid4().hex[:12])')"
database="$fixture-db"
wordpress="$fixture-web"

cleanup() {
    docker rm --force --volumes "$wordpress" "$database" >/dev/null 2>&1 || true
    docker network rm "$fixture" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$fixture" >/dev/null
docker run --detach --rm --name "$database" --network "$fixture" --network-alias database \
    --env MARIADB_RANDOM_ROOT_PASSWORD=1 --env MARIADB_DATABASE=wordpress \
    --env MARIADB_USER=wordpress --env MARIADB_PASSWORD=fixture-only \
    mariadb:11.4 >/dev/null

ready=false
for _ in {1..60}; do
    if docker exec "$database" healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    echo 'The disposable WordPress database did not start.' >&2
    exit 1
fi

docker run --detach --rm --name "$wordpress" --network "$fixture" --publish 127.0.0.1::80 \
    --env WORDPRESS_DB_HOST=database --env WORDPRESS_DB_NAME=wordpress \
    --env WORDPRESS_DB_USER=wordpress --env WORDPRESS_DB_PASSWORD=fixture-only \
    --env WP_ENVIRONMENT_TYPE=local --env 'WORDPRESS_CONFIG_EXTRA=define("WP_HTTP_BLOCK_EXTERNAL", true);' \
    wordpress:6.9.4-php8.4-apache >/dev/null
origin="http://$(docker port "$wordpress" 80/tcp)"

ready=false
for _ in {1..60}; do
    if curl --fail --silent --max-time 2 --output /dev/null "$origin/wp-admin/install.php"; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    echo 'The disposable WordPress web server did not start.' >&2
    exit 1
fi

credentials=$(docker exec --interactive --user www-data --env "STUDIO_FIXTURE_ORIGIN=$origin" \
    "$wordpress" php <<'PHP'
<?php
$origin = getenv('STUDIO_FIXTURE_ORIGIN');
$_SERVER['HTTP_HOST'] = substr($origin, strlen('http://'));
$_SERVER['REQUEST_URI'] = '/wp-admin/install.php';
define('WP_INSTALLING', true);
require '/var/www/html/wp-load.php';
require ABSPATH . 'wp-admin/includes/upgrade.php';

add_filter('pre_wp_mail', '__return_true');
wp_install('iTeam WordPress acceptance', 'fixture-admin', 'admin@example.test', false, '', bin2hex(random_bytes(24)));
update_option('home', $origin);
update_option('siteurl', $origin);
update_option('permalink_structure', '/%postname%/');
flush_rewrite_rules(false);

$author = wp_insert_user([
    'user_login' => 'studio-author',
    'user_email' => 'studio-author@example.test',
    'user_pass' => bin2hex(random_bytes(24)),
    'role' => 'author',
]);
if (is_wp_error($author)) {
    fwrite(STDERR, $author->get_error_message() . PHP_EOL);
    exit(1);
}
$password = WP_Application_Passwords::create_new_application_password($author, ['name' => 'iTeam acceptance']);
if (is_wp_error($password)) {
    fwrite(STDERR, $password->get_error_message() . PHP_EOL);
    exit(1);
}
printf("%d %s\n", $author, $password[0]);
PHP
)
read -r author_id application_password <<< "$credentials"
if [[ ! "$author_id" =~ ^[1-9][0-9]*$ || ! "$application_password" =~ ^[[:alnum:]]{24}$ ]]; then
    echo 'Could not provision the disposable WordPress application password.' >&2
    exit 1
fi

cd "$(dirname "$0")/../../backend"
ITEAM_WORDPRESS_ACCEPTANCE_URL="$origin" \
ITEAM_WORDPRESS_ACCEPTANCE_AUTHOR_ID="$author_id" \
ITEAM_WORDPRESS_ACCEPTANCE_PASSWORD="$application_password" \
    python -m pytest -q app/tests/test_campaign_studio.py::test_studio_reconciles_writes_on_a_real_wordpress_site
