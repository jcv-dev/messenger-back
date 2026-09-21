"""URL routes for the ops → Messager integration endpoints."""

from django.urls import path

from .views import ExemptionsSyncView, OrderEventsView, PingView, SlaAlertView

app_name = 'integrations'

urlpatterns = [
    path('ping/', PingView.as_view(), name='ping'),
    path('exemptions/sync/', ExemptionsSyncView.as_view(), name='exemptions-sync'),
    path('orders/events/', OrderEventsView.as_view(), name='orders-events'),
    path('notifications/sla/', SlaAlertView.as_view(), name='sla-alert'),
]
