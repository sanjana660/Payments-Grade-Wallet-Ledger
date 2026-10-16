# Schema design

The complete noun inventory, cardinalities, constraints, and ER diagram are documented in [README.md](../README.md). The executable schema is [001_initial.sql](../migrations/001_initial.sql).

The central invariant is that each completed transfer has equal positive debit and credit entry totals. Wallet balances are cached on `accounts`, protected by a non-negative trigger for wallet accounts, and reconciled from immutable entries.
