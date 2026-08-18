from django.urls import include, path
from rest_framework.routers import DefaultRouter

from api.chat import chat
from api.views import NoteViewSet, health

router = DefaultRouter()
router.register(r"notes", NoteViewSet, basename="note")

urlpatterns = [
    path("health/", health, name="health"),
    path("v1/chat", chat, name="chat"),
    path("", include(router.urls)),
]
