CREATE TABLE IF NOT EXISTS hourly_stock_slack_posts (
    channel text NOT NULL,
    check_hour timestamptz NOT NULL,
    pacific_day date NOT NULL,
    is_parent boolean NOT NULL,
    slack_ts text,
    PRIMARY KEY (channel, check_hour)
);

CREATE UNIQUE INDEX IF NOT EXISTS hourly_stock_slack_one_parent
    ON hourly_stock_slack_posts (channel, pacific_day)
    WHERE is_parent;
