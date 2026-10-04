# Final historical fraud feature design

## Scope and canonical audit

This document defines the final F4D design for point-in-time historical fraud
features. It does not implement Gold tables, Feast, training, serving, or model
preprocessing.

The design was checked against the F4C canonical population: 500,000 users,
674,383 accounts, 572,632 devices, and 4,000,000 logical transactions. The
population contains 80,000 fraud transactions split equally across velocity,
amount anomaly, account takeover, and merchant burst.

Four entity levels are necessary because they describe different behavior:

- **User** captures activity across all wallets owned by a person. There are
  149,709 multi-account users.
- **Account** captures wallet-specific behavior. A total of 305,879 accounts
  have a transaction population different from their owner's total, and 35,446
  accounts have no transactions. Account history is therefore no longer a
  redundant copy of user history.
- **Device** captures access-channel recency and activity. Normal traffic has
  46,602 transactions on devices at most seven days old, while account takeover
  has 12,832 of 20,000. Both populations have non-zero recent-device support,
  so recency is a risk signal rather than a deterministic fraud rule.
- **Merchant** captures shared payment-side behavior. The 15 merchants in the
  top 5% receive 79.06% of the 1,600,148 logical payment transactions, which
  makes a 24-hour traffic baseline necessary when interpreting short bursts.

The canonical data also contains same-entity transactions with identical event
timestamps. User, account, and device each have 90 such snapshot keys; merchant
payments have 64. This makes the shared same-timestamp rule operationally
relevant rather than merely theoretical.

## Point-in-time semantics

For a transaction evaluated at event time `T`, every historical window uses:

```text
[T - window, T)
```

The lower bound is inclusive and `T` is exclusive. The current transaction,
future events, and every other event at the same timestamp `T` are excluded.
Events sharing an entity and timestamp receive the same historical snapshot.
They must not be ordered by `transaction_id`, `ingested_at`, input position, or
any other processing artifact.

All event ordering and window boundaries use the Silver transaction business
`timestamp`. `ingested_at` must not create an artificial sequence. These rules
make the design point-in-time safe as long as feature computation preserves
the exclusive upper bound and the training join uses `event_timestamp` as its
as-of timestamp.

Unless a feature says otherwise, transaction aggregates include both
successful and failed historical transaction attempts. A failed attempt is
valid prior behavior. Transaction counts and amount sums therefore do not
default to successful transactions only. `failed_rate` is the feature that
uses historical `status`, with failed attempts as its numerator and all
historical attempts in the window as its denominator. For each entity, the
30-day amount observation count, average, and standard deviation use the exact
same set of historical transaction attempts in `[T - 30 days, T)`. Only the
current transaction status is blacklisted.

## Final atomic features

These features are historical aggregates or point-in-time entity attributes
that belong in persisted feature tables.

### User behavior

| Feature | Window | Reason |
|---|---|---|
| `user_tx_count_5m` | 5 minutes | Detects very short user-level velocity across accounts. |
| `user_tx_count_1h` | 1 hour | Captures sustained short-term activity without depending on one account. |
| `user_tx_count_24h` | 24 hours | Supplies the user's daily activity context. |
| `user_amount_sum_1h` | 1 hour | Measures short-term attempted value across all user accounts. |
| `user_amount_observation_count_30d` | 30 days | Makes amount-statistic support explicit for cold start and model preprocessing. |
| `user_avg_amount_30d` | 30 days | Provides the historical mean needed to compare request amount. |
| `user_std_amount_30d` | 30 days | Provides dispersion without using the current amount. |
| `user_failed_rate_24h` | 24 hours | Captures prior failure behavior; only status from events before `T` is used. |
| `user_distinct_merchants_24h` | 24 hours | Measures recent payment breadth using non-null merchant IDs. |

### Account behavior

| Feature | Window | Reason |
|---|---|---|
| `account_tx_count_5m` | 5 minutes | Detects wallet-specific velocity that user aggregation can dilute. |
| `account_tx_count_1h` | 1 hour | Supports account/user activity-share comparison. |
| `account_tx_count_24h` | 24 hours | Establishes daily account activity independent of sibling accounts. |
| `account_amount_sum_1h` | 1 hour | Captures short-term attempted value on the selected wallet. |
| `account_amount_sum_24h` | 24 hours | Distinguishes current account usage from the user's total wallet activity. |
| `account_amount_observation_count_30d` | 30 days | Exposes whether account amount statistics have enough support. |
| `account_avg_amount_30d` | 30 days | Supplies the account-relative amount baseline used by anomaly scenarios. |
| `account_std_amount_30d` | 30 days | Supplies account-specific dispersion for request-time anomaly derivation. |
| `account_failed_rate_24h` | 24 hours | Captures prior account failure behavior without leaking current status. |
| `account_seconds_since_last_tx` | Previous event | Detects abrupt reuse or rapid succession on the selected account. |

No balance-relative feature is included in V1. The current synthetic balance
semantics are sufficient for row-level consistency checks but have not been
shown to provide a stable, defensible fraud baseline across deposits, failed
attempts, payments, withdrawals, and transfers.

### Device behavior

| Feature | Window | Reason |
|---|---|---|
| `device_age_seconds` | As of `T` | Measures `T - first_seen_at`; recent devices remain risky but are not fraud-deterministic. |
| `device_tx_count_1h` | 1 hour | Captures rapid activity on the presented device. |
| `device_tx_count_24h` | 24 hours | Establishes daily device familiarity and activity. |
| `device_amount_sum_24h` | 24 hours | Captures value attempted through the device. |
| `device_failed_rate_24h` | 24 hours | Captures prior failed behavior on the device. |

`device_age_seconds` is non-negative in the canonical population. It is
computed from the request event time and the Silver device's
`first_seen_at`; it never uses labels or future transactions.

### Merchant behavior

Merchant snapshots are created only for payment transactions with a non-null
`merchant_id`. Non-payment transactions do not receive or manufacture a
merchant feature row.

| Feature | Window | Reason |
|---|---|---|
| `merchant_tx_count_10m` | 10 minutes | Matches the short merchant-burst horizon. |
| `merchant_tx_count_1h` | 1 hour | Captures a broader active burst. |
| `merchant_tx_count_24h` | 24 hours | Provides the traffic baseline required by the strongly skewed merchant population. |
| `merchant_unique_users_10m` | 10 minutes | Distinguishes a multi-user burst from repeated activity by one user. |
| `merchant_unique_users_1h` | 1 hour | Measures sustained breadth of merchant traffic. |
| `merchant_amount_sum_1h` | 1 hour | Captures short-term payment value. |
| `merchant_avg_amount_24h` | 24 hours | Provides the merchant's recent typical payment amount. |

## Derived features

Derived request comparisons are not persisted in the four historical tables.
They are inexpensive combinations of request-time fields and atomic historical
features. Keeping them in the training/serving transformation layer avoids
duplicated storage, preserves one formula for offline and online scoring, and
allows denominator handling to be versioned with the model.

| Derived feature | Inputs | Compute where |
|---|---|---|
| `amount_ratio_to_user_avg_30d` | Request `amount`, `user_avg_amount_30d` | Shared training/serving transformation |
| `amount_zscore_user_30d` | Request `amount`, `user_avg_amount_30d`, `user_std_amount_30d` | Shared training/serving transformation |
| `amount_ratio_to_account_avg_30d` | Request `amount`, `account_avg_amount_30d` | Shared training/serving transformation |
| `amount_zscore_account_30d` | Request `amount`, `account_avg_amount_30d`, `account_std_amount_30d` | Shared training/serving transformation |
| `account_tx_share_1h` | `account_tx_count_1h`, `user_tx_count_1h` | Shared training/serving transformation |
| `merchant_activity_ratio_10m_to_24h` | `merchant_tx_count_10m`, `merchant_tx_count_24h` with window-length normalization | Shared training/serving transformation |

Ratios and z-scores return `NULL` when their denominator is absent or zero.
Any later imputation belongs to model preprocessing, not feature computation.

## Leakage blacklist

Feature computation must never use:

- `label` or `fraud_type`;
- current transaction `status`;
- current transaction `new_balance`;
- the current transaction in any rolling aggregate;
- another transaction for the same entity at the same timestamp;
- future events;
- historical fraud labels;
- full-dataset aggregates without a point-in-time cutoff;
- `ingested_at` to break event-time ties.

Historical `status` is allowed only when its transaction timestamp is strictly
less than `T`. Request-time fields such as `amount`, `user_id`, `account_id`,
`device_id`, `merchant_id`, transaction type, and channel remain model inputs,
but they are not copied into historical aggregates unless explicitly listed.

## Cold-start and null semantics

Feature computation does not silently impute:

| Condition | Stored value |
|---|---|
| No history for a count | `0` |
| No history for a sum | `0` |
| No denominator for an average | `NULL` |
| No denominator for a rate | `NULL` |
| Fewer than two observations for standard deviation | `NULL` |
| No previous account transaction | `NULL` for `account_seconds_since_last_tx` |
| Missing or zero denominator for a derived ratio/z-score | `NULL` |

Observation-count features let downstream preprocessing distinguish a genuine
numeric baseline from cold start. Any zero/median/sentinel imputation is a
later model decision and must be learned or configured without future data.

## Proposed feature tables

All tables are event-driven snapshots. A key is emitted once per distinct
`(entity_id, event_timestamp)` present in its eligible Silver transaction
population. Multiple transactions for the same entity at the same timestamp
share the row. `source_transaction_id` is intentionally absent because it
would contradict this grain.

| Table | Entity key | Grain | Features | Silver sources |
|---|---|---|---:|---|
| `feat_user_behavior` | `user_id` | One row per `user_id, event_timestamp` | 9 | `transactions` |
| `feat_account_behavior` | `account_id` | One row per `account_id, event_timestamp` | 10 | `transactions` |
| `feat_device_behavior` | `device_id` | One row per `device_id, event_timestamp` | 5 | `transactions`, `devices` |
| `feat_merchant_behavior` | `merchant_id` | One row per payment `merchant_id, event_timestamp` | 7 | `transactions` |

Every table contains its entity key, `event_timestamp`, and only the listed
atomic features. The temporal rule is `[T - window, T)` for windowed features.
`account_seconds_since_last_tx` uses the greatest account timestamp strictly
less than `T`; `device_age_seconds` uses `T - first_seen_at`. Null behavior is
the shared policy above.

## Fraud-scenario coverage

| Fraud scenario | Historical features | Request-time fields | Derived features |
|---|---|---|---|
| Velocity | User/account counts at 5m, 1h, and 24h; account time since last transaction | `user_id`, `account_id`, `timestamp`, request amount | `account_tx_share_1h` |
| Amount anomaly | User/account 30d observation count, average, and standard deviation; recent amount sums | Request `amount`, entity IDs, `timestamp` | User/account amount ratios and z-scores |
| Account takeover | Device age, device counts/value/failure rate; account velocity, amount statistics, and time since last transaction | `device_id`, `account_id`, `user_id`, channel, amount, `timestamp` | Account amount ratio/z-score and account activity share |
| Merchant burst | Merchant 10m/1h counts, unique users, 1h value, 24h count and average | Payment `merchant_id`, `user_id`, amount, `timestamp` | `merchant_activity_ratio_10m_to_24h` |

All four generated scenarios have observable historical and request-time
signals. Account takeover relies on a combination: device recency alone cannot
determine fraud because normal and takeover age distributions overlap.

## Feast readiness

The design is compatible with future point-in-time historical retrieval:

| Future Feast concept | Proposed mapping |
|---|---|
| Entity | `user_id`, `account_id`, `device_id`, `merchant_id` |
| FeatureViews | `user_behavior`, `account_behavior`, `device_behavior`, `merchant_behavior` |
| Timestamp field | `event_timestamp` |
| Historical join | Latest eligible entity snapshot at or before the request time, where every snapshot itself was computed with an exclusive `T` boundary |

Offline construction must compute same-timestamp snapshots before joining them
back to transactions. Online materialization can later publish the latest
completed snapshot per entity. Training and serving must share the derived
feature formulas and null handling.

This is a design-readiness conclusion only. Feast is not installed or
configured by F4D.
