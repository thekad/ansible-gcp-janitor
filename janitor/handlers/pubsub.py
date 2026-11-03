from __future__ import annotations

import logging
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix


def handle_pubsub(project: str, prefix: str, do_cleanup: bool) -> HandlerResult:
    from google.cloud import pubsub_v1

    publisher_client = pubsub_v1.PublisherClient()
    subscriber_client = pubsub_v1.SubscriberClient()
    found: list[dict[str, str]] = []
    actions: list[str] = []

    parent = f"projects/{project}"

    # Delete subscriptions first (they depend on topics)
    try:
        subscription_tasks: list[T.Callable[[], None]] = []
        for subscription in subscriber_client.list_subscriptions(
            request={"project": parent}
        ):
            if not match_prefix(subscription.name, prefix):
                continue
            found.append({"type": "pubsub.Subscription", "name": subscription.name})
            if do_cleanup:

                def _delete_subscription(
                    subscription_name: str = subscription.name,
                ) -> None:
                    try:
                        subscriber_client.delete_subscription(
                            request={"subscription": subscription_name}
                        )
                        actions.append(
                            f"Deleted PubSub Subscription: {subscription_name}"
                        )
                    except Exception as e:
                        msg = f"Could not delete PubSub Subscription {subscription_name}: {e}"
                        logging.warning(msg)
                        actions.append(f"[WARN] {msg}")

                subscription_tasks.append(_delete_subscription)
        _run_concurrent(subscription_tasks)
    except Exception as e:
        msg = f"PubSub subscription list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    # Delete topics after subscriptions are gone
    try:
        topic_tasks: list[T.Callable[[], None]] = []
        for topic in publisher_client.list_topics(request={"project": parent}):
            if not match_prefix(topic.name, prefix):
                continue
            found.append({"type": "pubsub.Topic", "name": topic.name})
            if do_cleanup:

                def _delete_topic(topic_name: str = topic.name) -> None:
                    try:
                        publisher_client.delete_topic(request={"topic": topic_name})
                        actions.append(f"Deleted PubSub Topic: {topic_name}")
                    except Exception as e:
                        msg = f"Could not delete PubSub Topic {topic_name}: {e}"
                        logging.warning(msg)
                        actions.append(f"[WARN] {msg}")

                topic_tasks.append(_delete_topic)
        _run_concurrent(topic_tasks)
    except Exception as e:
        msg = f"PubSub topic list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    return {"found": found, "actions": actions}
