"""Đăng ký các Delta table đã tồn tại vào Trino metastore.

Chạy từ repository root:

    python3 -m data_platform.storage.scripts.register_trino_tables --layer silver
    python3 -m data_platform.storage.scripts.register_trino_tables --layer gold
    python3 -m data_platform.storage.scripts.register_trino_tables --layer all

Script chỉ thay đổi metadata Trino. Nó không ghi hoặc thay đổi Delta data.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from data_platform.config import RuntimeConfig, load_runtime_config
from data_platform.storage.scripts.config import (
    TRINO_CATALOG,
    registration_table_uri,
    require_local_runtime,
    trino_registration_layers,
)
from data_platform.storage.scripts.minio_client import (
    MinioStorageError,
    check_minio_connection,
    create_minio_client,
    delta_log_exists,
)
from data_platform.storage.scripts.trino_client import (
    TrinoClientError,
    check_trino_connection,
    ensure_schema,
    get_table_location,
    list_schema_tables,
    normalize_table_location,
    register_delta_table,
    trino_cursor,
    verify_table_readable,
)


logger = logging.getLogger(__name__)


class TrinoRegistrationError(RuntimeError):
    """Lỗi preflight hoặc completeness của registration flow."""


@dataclass(frozen=True)
class RegistrationResult:
    layer: str
    schema: str
    table: str
    location: str
    registered_now: bool


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("trino").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Register existing Bronze, Silver, and Gold Delta tables in Trino."
        )
    )
    parser.add_argument(
        "--environment",
        choices=["local", "dev", "gcp"],
        default="local",
        help="Runtime profile; this command only accepts local.",
    )
    parser.add_argument(
        "--layer",
        choices=("bronze", "silver", "gold", "all"),
        default="all",
        help="Layer to register. 'all' processes Bronze, Silver, and Gold.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help=(
            "Require and register Bronze/Silver fraud_labels in transitional "
            "fraud mode."
        ),
    )
    return parser.parse_args(argv)


def select_layers(layer: str) -> tuple[str, ...]:
    if layer == "all":
        return ("bronze", "silver", "gold")
    return (layer,)


def verify_delta_sources(
    minio_client,
    layers: tuple[str, ...],
    registration_layers,
) -> None:
    """Fail trước mọi metadata mutation nếu một Delta source bị thiếu."""
    missing: list[str] = []

    for layer in layers:
        layer_config = registration_layers[layer]
        bucket = urlsplit(str(layer_config["root"])).netloc

        for table in layer_config["tables"]:
            location = registration_table_uri(layer_config, table)

            if delta_log_exists(minio_client, bucket, table):
                logger.info(
                    "SOURCE VERIFIED | layer=%s | table=%s | location=%s",
                    layer,
                    table,
                    location,
                )
            else:
                logger.error(
                    "SOURCE MISSING | layer=%s | table=%s | location=%s",
                    layer,
                    table,
                    location,
                )
                missing.append(location)

    if missing:
        raise TrinoRegistrationError(
            "DELTA SOURCE MISSING: " + ", ".join(missing)
        )


def register_layer(
    cursor,
    layer: str,
    registration_layers,
) -> list[RegistrationResult]:
    layer_config = registration_layers[layer]
    schema = str(layer_config["schema"])
    root = str(layer_config["root"])
    expected_tables = set(layer_config["tables"])
    results: list[RegistrationResult] = []

    ensure_schema(
        cursor=cursor,
        schema_name=schema,
        location_root=root,
    )

    for table in layer_config["tables"]:
        expected_location = registration_table_uri(layer_config, table)
        registered_now = register_delta_table(
            cursor=cursor,
            table_name=table,
            schema_name=schema,
            table_location=expected_location,
        )
        actual_location = get_table_location(cursor, schema, table)

        if normalize_table_location(actual_location or "") != (
            normalize_table_location(expected_location)
        ):
            raise TrinoRegistrationError(
                "LOCATION MISMATCH sau registration cho "
                f"{TRINO_CATALOG}.{schema}.{table}: "
                f"actual={actual_location!r}, expected={expected_location!r}"
            )

        verify_table_readable(cursor, schema, table)
        action = "REGISTERED" if registered_now else "ALREADY PRESENT"
        logger.info(
            "%s | layer=%s | table=%s | location=%s",
            action,
            layer,
            table,
            actual_location,
        )
        logger.info(
            "VERIFIED | layer=%s | table=%s",
            layer,
            table,
        )
        results.append(
            RegistrationResult(
                layer=layer,
                schema=schema,
                table=table,
                location=actual_location or expected_location,
                registered_now=registered_now,
            )
        )

    actual_tables = list_schema_tables(cursor, schema)
    missing_tables = sorted(expected_tables - actual_tables)
    unexpected_tables = sorted(actual_tables - expected_tables)

    logger.info(
        "TABLE SET | layer=%s | expected=%d | actual=%d | missing=%s | "
        "unexpected=%s",
        layer,
        len(expected_tables),
        len(actual_tables),
        missing_tables,
        unexpected_tables,
    )

    if missing_tables:
        raise TrinoRegistrationError(
            f"Thiếu table trong {TRINO_CATALOG}.{schema}: {missing_tables}"
        )

    if unexpected_tables:
        logger.warning(
            "UNEXPECTED TABLES | layer=%s | tables=%s",
            layer,
            unexpected_tables,
        )

    return results


def run(
    layer: str,
    include_fraud_labels: bool = False,
    runtime_config: RuntimeConfig | None = None,
) -> int:
    config = runtime_config or load_runtime_config()
    require_local_runtime(config, "register_trino_tables")
    layers = select_layers(layer)
    registration_layers = trino_registration_layers(
        include_fraud_labels,
        runtime_config=config,
    )
    minio_client = create_minio_client(config)
    check_minio_connection(minio_client)
    verify_delta_sources(minio_client, layers, registration_layers)

    results: list[RegistrationResult] = []

    with trino_cursor(config) as cursor:
        check_trino_connection(cursor)

        for selected_layer in layers:
            results.extend(
                register_layer(cursor, selected_layer, registration_layers)
            )

    registered = sum(result.registered_now for result in results)
    already_present = len(results) - registered
    logger.info(
        "REGISTRATION COMPLETE | tables=%d | registered=%d | "
        "already_present=%d",
        len(results),
        registered,
        already_present,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)

    try:
        if args.environment != "local":
            raise RuntimeError(
                "register_trino_tables is a local-only command; "
                f"selected environment={args.environment!r}"
            )
        config = load_runtime_config(args.environment)
        return run(
            args.layer,
            include_fraud_labels=args.include_fraud_labels,
            runtime_config=config,
        )
    except (
        MinioStorageError,
        TrinoClientError,
        TrinoRegistrationError,
    ):
        logger.exception("REGISTRATION FAILED")
        return 1
    except KeyboardInterrupt:
        logger.warning("Registration interrupted.")
        return 130
    except Exception:
        logger.exception("REGISTRATION FAILED: unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
