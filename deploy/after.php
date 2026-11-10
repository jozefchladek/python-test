<?php
// Testovaci skript "Spustenie skriptu po nasadeni" (php). Posledny parameter "fail" = skonci chybou.
$args = array_slice($argv, 1);

echo "after.php: bezi\n";
echo '  pouzivatel: ' . (function_exists('posix_getpwuid') ? posix_getpwuid(posix_geteuid())['name'] : get_current_user()) . "\n";
echo '  adresar:    ' . getcwd() . "\n";
echo '  php:        ' . PHP_VERSION . "\n";
echo '  parametre:  ' . count($args) . ' -> ' . implode(' ', $args) . "\n";
echo '  .env:       ' . (is_file('.env') ? 'existuje' : 'chyba') . "\n";

if (end($args) === 'fail') {
    echo "after.php: koncim chybou\n";
    exit(3);
}
