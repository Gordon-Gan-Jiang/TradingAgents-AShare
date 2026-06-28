### Requirement: Paper portfolio lifecycle
The system SHALL maintain per-user paper portfolios with cash balance, positions, and trade history.

#### Scenario: Bootstrap from imported positions
- **WHEN** a user bootstraps paper trading from portfolio import
- **THEN** paper positions MUST mirror imported symbols with configurable initial cash

#### Scenario: Execute paper trade
- **WHEN** a valid buy/sell request is submitted
- **THEN** cash and positions MUST update atomically and a trade record MUST be created

### Requirement: Daily operation plan
The system MAY generate and optionally auto-execute a daily operation plan from recommendation output during scheduled jobs.

#### Scenario: Daily review record
- **WHEN** the daily review job completes for a trade date
- **THEN** a `DailyReviewDB` row MUST capture summary metrics for that user and date
