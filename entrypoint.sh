#!/bin/sh
set -e
python manage.py collectstatic --noinput
python manage.py migrate --noinput
mkdir -p /app/media
chown -R www-data:www-data /app/media /app/staticfiles
exec supervisord -c /etc/supervisor/conf.d/supervisord.conf