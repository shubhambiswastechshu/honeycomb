#!/usr/bin/env bash
# Render build step for the Django service. Runs from Honeycomb/ (rootDir).
#
# set -o errexit matters here: without it a failed migration still exits 0 and
# Render happily starts a service against a half-migrated database.
set -o errexit
set -o pipefail
set -o nounset

pip install --upgrade pip
pip install -r requirements.txt

# --no-input because there is no tty; --clear so a renamed asset cannot linger
# in the manifest and get served forever under its old hashed name.
python manage.py collectstatic --no-input --clear

python manage.py migrate
