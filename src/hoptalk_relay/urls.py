from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

# staticfiles_urlpatterns() is empty unless DEBUG is on: in production nginx serves the
# collected files itself.
urlpatterns = [
    path("", include("panel.urls")),
    *staticfiles_urlpatterns(),
]
