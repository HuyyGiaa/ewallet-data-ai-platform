"""
Sinh transaction event liên tục, đẩy vào Kafka topic transactions.raw.

Chạy: python streaming_generator.py
(đọc config từ ../../config/settings.yaml, giống offline_generator.py)

Ctrl+C để dừng.
"""

import argparse
import json
import random
import time
from datetime import datetime, time as dt_time
from pathlib import Path

import yaml
from confluent_kafka import Producer

from .constants import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC
from .state_loader import load_state
from .event_factory import build_event


def load_cfg() -> dict:
    # data_generator/src/streaming/streaming_generator.py -> parents[2] = data_generator/
    base_dir = Path(__file__).resolve().parents[2]
    cfg_path = base_dir / "config" / "settings.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_window(window_str: str) -> tuple[dt_time, dt_time]:
    start_str, end_str = window_str.split("-")
    start_t = datetime.strptime(start_str.strip(), "%H:%M").time()
    end_t = datetime.strptime(end_str.strip(), "%H:%M").time()
    return start_t, end_t


def get_current_rate(now: datetime, cfg: dict) -> float:
    """Trả về số event/phút kỳ vọng tại thời điểm now (áp dụng burst nếu đang trong window)."""
    base_rate = cfg.get("base_events_per_min", 50)
    multiplier = cfg.get("burst_multiplier", 30)
    windows = cfg.get("burst_windows", [])

    now_t = now.time()
    for window_str in windows:
        start_t, end_t = _parse_window(window_str)
        if start_t <= now_t <= end_t:
            return base_rate * multiplier
    return base_rate


def delivery_report(err, msg):
    if err:
        print(f"Delivery failed: {err}")
    # Không log mỗi lần thành công để tránh spam terminal khi rate cao (burst 1500/phút)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-servers", type=str, default=KAFKA_BOOTSTRAP_SERVERS)
    parser.add_argument("--topic", type=str, default=KAFKA_TOPIC)
    args = parser.parse_args()

    cfg = load_cfg()
    state = load_state(cfg)

    producer = Producer({"bootstrap.servers": args.bootstrap_servers})

    duplicate_rate = cfg.get("duplicate_rate_stream", 0.015)
    dup_delay_range = (60, 180)  # 1-3 phút, tính bằng giây

    # Hàng đợi duplicate: list các (send_at_epoch_seconds, event_dict)
    pending_duplicates: list[tuple[float, dict]] = []

    sent_count = 0
    print(f"Bắt đầu sinh event vào topic '{args.topic}'. Ctrl+C để dừng.")

    try:
        while True:
            now = datetime.now()

            # --- 1. Gửi lại các duplicate đã tới hạn (nếu có) ---
            still_pending = []
            for send_at, dup_event in pending_duplicates:
                if time.time() >= send_at:
                    producer.produce(
                        args.topic,
                        value=json.dumps(dup_event).encode("utf-8"),
                        on_delivery=delivery_report,
                    )
                    sent_count += 1
                else:
                    still_pending.append((send_at, dup_event))
            pending_duplicates = still_pending

            # --- 2. Sinh và gửi 1 event mới ---
            event = build_event(state, now)
            producer.produce(
                args.topic,
                value=json.dumps(event).encode("utf-8"),
                on_delivery=delivery_report,
            )
            sent_count += 1
            producer.poll(0)  # xử lý callback không chặn vòng lặp

            # --- 3. Có thể lên lịch gửi duplicate cho event vừa tạo ---
            if random.random() < duplicate_rate:
                delay_seconds = random.randint(*dup_delay_range)
                pending_duplicates.append((time.time() + delay_seconds, dict(event)))

            if sent_count % 200 == 0:
                rate_now = get_current_rate(now, cfg)
                print(f"Đã gửi {sent_count} event | rate hiện tại: {rate_now}/phút | pending dup: {len(pending_duplicates)}")

            # --- 4. Nghỉ theo đúng rate hiện tại (baseline hoặc burst) ---
            rate_per_min = get_current_rate(now, cfg)
            rate_per_sec = rate_per_min / 60.0
            time.sleep(random.expovariate(rate_per_sec))

    except KeyboardInterrupt:
        print(f"\nDừng lại. Tổng cộng đã gửi {sent_count} event.")
    finally:
        producer.flush()


if __name__ == "__main__":
    main()