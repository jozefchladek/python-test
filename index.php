<?php

$counterFile = '/data/counter.txt';

if (!is_dir(dirname($counterFile))) {
    mkdir(dirname($counterFile), 0777, true);
}

$count = 0;
if (file_exists($counterFile)) {
    $count = (int) trim(file_get_contents($counterFile));
}
$count++;
file_put_contents($counterFile, (string) $count);

header('Content-Type: text/plain; charset=utf-8');

echo "Hello from GHCR test image!\n";
echo "Hit count (persisted in /data volume): {$count}\n";
echo 'Time: ' . gmdate('c') . "\n";
