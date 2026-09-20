"""URL routes for the ops → Messager integration endpoints."""

from django.urls import path

from .views import ExemptionsSyncView, OrderEventsView, PingView

app_name = 'integrations'

urlpatterns = [
    path('ping/', PingView.as_view(), name='ping'),
    path('exemptions/sync/', ExemptionsSyncView.as_view(), name='exemptions-sync'),
    path('orders/events/', OrderEventsView.as_view(), name='orders-events'),
]
