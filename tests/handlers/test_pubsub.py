"""Unit tests for the PubSub handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.pubsub import handle_pubsub


def _mock_pubsub_module(
    monkeypatch: pytest.MonkeyPatch,
    publisher_client: MagicMock,
    subscriber_client: MagicMock,
) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    pubsub_v1 = ModuleType("google.cloud.pubsub_v1")

    pubsub_v1.PublisherClient = MagicMock(return_value=publisher_client)
    pubsub_v1.SubscriberClient = MagicMock(return_value=subscriber_client)

    google.cloud = cloud
    cloud.pubsub_v1 = pubsub_v1

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.pubsub_v1", pubsub_v1)


@pytest.mark.unit
@pytest.mark.handler
class TestHandlePubSub:
    def test_cleanup_deletes_matching_subscriptions_and_topics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publisher_client = MagicMock()
        subscriber_client = MagicMock()

        subscriber_client.list_subscriptions.return_value = [
            SimpleNamespace(
                name="projects/test-project/subscriptions/ansible-test-subscription"
            ),
            SimpleNamespace(
                name="projects/test-project/subscriptions/prod-subscription"
            ),
        ]
        publisher_client.list_topics.return_value = [
            SimpleNamespace(name="projects/test-project/topics/ansible-test-topic"),
            SimpleNamespace(name="projects/test-project/topics/prod-topic"),
        ]

        _mock_pubsub_module(monkeypatch, publisher_client, subscriber_client)

        result = handle_pubsub("test-project", "ansible-test-", True)

        subscriber_client.delete_subscription.assert_called_once_with(
            request={
                "subscription": "projects/test-project/subscriptions/ansible-test-subscription"
            }
        )
        publisher_client.delete_topic.assert_called_once_with(
            request={"topic": "projects/test-project/topics/ansible-test-topic"}
        )
        assert result["found"] == [
            {
                "type": "pubsub.Subscription",
                "name": "projects/test-project/subscriptions/ansible-test-subscription",
            },
            {
                "type": "pubsub.Topic",
                "name": "projects/test-project/topics/ansible-test-topic",
            },
        ]

    def test_subscription_list_failure_is_reported_and_topic_scan_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publisher_client = MagicMock()
        subscriber_client = MagicMock()

        subscriber_client.list_subscriptions.side_effect = RuntimeError(
            "subscription list failed"
        )
        publisher_client.list_topics.return_value = [
            SimpleNamespace(name="projects/test-project/topics/ansible-test-topic")
        ]

        _mock_pubsub_module(monkeypatch, publisher_client, subscriber_client)

        result = handle_pubsub("test-project", "ansible-test-", True)

        publisher_client.delete_topic.assert_called_once_with(
            request={"topic": "projects/test-project/topics/ansible-test-topic"}
        )
        assert result["actions"] == [
            "[WARN] PubSub subscription list failed: subscription list failed",
            "Deleted PubSub Topic: projects/test-project/topics/ansible-test-topic",
        ]
