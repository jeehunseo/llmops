"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from config.otel import setup_tracing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

setup_tracing()

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
