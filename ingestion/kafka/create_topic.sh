#!/bin/bash
# Chạy sau khi `docker compose up -d` đã xong (đợi vài giây cho Redpanda sẵn sàng).
# Cách chạy: bash create_topic.sh

TOPIC="transactions.raw"
PARTITIONS=3          # đủ để demo song song hóa, không cần nhiều hơn cho quy mô coursework
REPLICATION=1         # single-node, không cần replication factor cao

docker exec -it redpanda rpk topic create "$TOPIC" \
  --partitions "$PARTITIONS" \
  --replicas "$REPLICATION"

echo "Danh sách topic hiện có:"
docker exec -it redpanda rpk topic list
