"""Concrete online-topic consumer using Feast's supported push boundary."""

from __future__ import annotations

import argparse

from data_platform.streaming.contracts.topics import CONSUMER_GROUPS, TOPICS
from data_platform.streaming.feast.online import FeastPushWriter
from data_platform.streaming.sinks.online import OnlineFeatureUpdate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--feast-repo", required=True)
    args = parser.parse_args(argv)
    from confluent_kafka import Consumer
    from feast import FeatureStore

    consumer = Consumer({"bootstrap.servers": args.bootstrap_servers, "group.id": CONSUMER_GROUPS["feast"], "enable.auto.commit": False, "auto.offset.reset": "earliest"})
    consumer.subscribe([TOPICS["online"].name])
    writer = FeastPushWriter(FeatureStore(repo_path=args.feast_repo))
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise RuntimeError(str(message.error()))
            writer.write(OnlineFeatureUpdate.from_json(message.value()))
            consumer.commit(message=message, asynchronous=False)
    finally:
        consumer.close()


if __name__ == "__main__":
    raise SystemExit(main())
