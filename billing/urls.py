from django.urls import path
from .views import (
    PlanListView,
    CurrentSubscriptionView,
    CreateSubscriptionView,
    CancelSubscriptionView,
    RazorpayWebhookView,
    UsageView,
)

urlpatterns = [
    path("plans/", PlanListView.as_view(), name="billing-plans"),
    path("subscription/", CurrentSubscriptionView.as_view(), name="billing-subscription"),
    path("subscribe/", CreateSubscriptionView.as_view(), name="billing-subscribe"),
    path("cancel/", CancelSubscriptionView.as_view(), name="billing-cancel"),
    path("webhook/", RazorpayWebhookView.as_view(), name="billing-webhook"),
    path("usage/", UsageView.as_view(), name="billing-usage"),
]
