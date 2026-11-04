# Account takeover review policy

This demonstration policy requires step-up review when a transaction follows a new-device login, a rapid credential reset, or an impossible-travel signal. Analysts must review the account history, device binding, recent failed login count, and the freshness of the underlying features before deciding. A model score alone must not close an account-takeover case. Evidence presented to an analyst must include the source policy identifier and the exact policy revision.

If required features are missing, stale, or inconsistent, the decision must fail closed to manual review. Services must not log raw credentials, payment account numbers, secrets, or hidden model reasoning. Any override requires an authenticated reviewer, a reason code, and an auditable timestamp.
