from datetime import timedelta

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from .models import OAuthState, Post, SocialAccount
from .serializers import PostSerializer, SocialAccountSerializer
from .views import SocialConnectStartView

User = get_user_model()


class PostSerializerTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username="tester",
            email="test@example.com",
            password="strongpass123",
        )
        SocialAccount.objects.create(
            user=self.user,
            platform=SocialAccount.Platform.FACEBOOK,
            access_token="token",
            account_id="acct-1",
        )

    def _request(self):
        request = self.factory.post("/api/posts/")
        request.user = self.user
        return request

    def test_requires_connected_accounts(self):
        serializer = PostSerializer(
            data={"content": "hello", "platforms": ["facebook", "linkedin"]},
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)

    def test_rejects_past_schedule(self):
        serializer = PostSerializer(
            data={
                "content": "hello",
                "platforms": ["facebook"],
                "scheduled_time": timezone.now() - timedelta(minutes=5),
            },
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("scheduled_time", serializer.errors)

    def test_normalizes_and_deduplicates_platforms(self):
        serializer = PostSerializer(
            data={"content": "hello", "platforms": [" Facebook ", "facebook"]},
            context={"request": self._request()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["platforms"], ["facebook"])

    def test_rejects_instagram_without_image(self):
        SocialAccount.objects.create(
            user=self.user,
            platform=SocialAccount.Platform.INSTAGRAM,
            access_token="token-ig",
            account_id="acct-ig",
        )
        serializer = PostSerializer(
            data={"content": "hello", "platforms": ["instagram"]},
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)

    def test_accepts_instagram_with_image(self):
        SocialAccount.objects.create(
            user=self.user,
            platform=SocialAccount.Platform.INSTAGRAM,
            access_token="token-ig",
            account_id="acct-ig",
        )
        serializer = PostSerializer(
            data={
                "content": "hello",
                "image": "https://example.com/image.jpg",
                "platforms": ["instagram"],
            },
            context={"request": self._request()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class SocialAccountSerializerTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username="tester2",
            email="test2@example.com",
            password="strongpass123",
        )
        self.account = SocialAccount.objects.create(
            user=self.user,
            platform=SocialAccount.Platform.LINKEDIN,
            access_token="token",
            account_id="acct-2",
        )

    def _request(self, method="post"):
        request_factory = getattr(self.factory, method)
        request = request_factory("/api/social-accounts/")
        request.user = self.user
        return request

    def test_duplicate_platform_is_rejected(self):
        serializer = SocialAccountSerializer(
            data={
                "platform": "linkedin",
                "access_token": "new-token",
            },
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)

    def test_update_same_account_is_allowed(self):
        serializer = SocialAccountSerializer(
            self.account,
            data={"refresh_token": "new-refresh"},
            partial=True,
            context={"request": self._request(method="put")},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_platform_is_normalized(self):
        serializer = SocialAccountSerializer(
            data={
                "platform": " LinkedIn ",
                "access_token": "token-x",
            },
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)


class SocialConnectStartViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username="oauth-user",
            email="oauth@example.com",
            password="strongpass123",
        )

    def test_instagram_connect_note_mentions_meta_facebook_login(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        with patch("core.views.generate_state", return_value="state-123"), \
             patch("core.views.oauth_expiry", return_value=timezone.now() + timedelta(minutes=10)), \
             patch("core.views.build_meta_auth_url", return_value="https://facebook.example/oauth"):
            response = client.get("/api/social-connect/instagram/start/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Meta/Facebook", response.json()["note"])
        self.assertIn("Instagram", response.json()["note"])
        self.assertTrue(OAuthState.objects.filter(user=self.user, platform="instagram").exists())


class PostModelTests(TestCase):
    def test_platform_results_default(self):
        user = User.objects.create_user(
            username="tester3",
            email="test3@example.com",
            password="strongpass123",
        )
        post = Post.objects.create(user=user, content="hello", platforms=["facebook"])
        self.assertEqual(post.platform_results, {})
