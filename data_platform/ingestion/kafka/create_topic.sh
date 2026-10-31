#!/bin/bash
# Chạy sau canonical split-Compose command trong README (đợi Redpanda sẵn sàng).
# Cách chạy: bash create_topic.sh

TOPIC="transactions.raw"
PARTITIONS=3
REPLICATION=1

docker exec -it redpanda rpk topic create "$TOPIC" \
  --partitions "$PARTITIONS" \
  --replicas "$REPLICATION"

echo "Danh sách topic hiện có:"
docker exec -it redpanda rpk topic list
