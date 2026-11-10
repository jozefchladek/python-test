#!/bin/bash
# Testovaci skript "Spustenie skriptu po nasadeni" (bash). Posledny parameter "fail" = skonci chybou.
echo "after.sh: bezi"
echo "  pouzivatel: $(id -un)"
echo "  adresar:    $(pwd -P)"
echo "  parametre:  $# -> $*"
echo "  .env:       $([ -f .env ] && echo existuje || echo chyba)"

if [ "${*: -1}" = "fail" ]; then
    echo "after.sh: koncim chybou"
    exit 3
fi
