import atexit
import os

from django.core.wsgi import get_wsgi_application

from hoptalk_relay.database_pools import close_database_pools

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hoptalk_relay.settings")

application = get_wsgi_application()

# A gunicorn worker exits through sys.exit(), which runs this before interpreter shutdown.
atexit.register(close_database_pools)
