CREATE TABLE obligations (
    company TEXT NOT NULL,
    grp TEXT NOT NULL CHECK (grp IN ('new_entrant', 'prime')),
    fy INTEGER NOT NULL,
    pricing_code TEXT NOT NULL,
    pricing_group TEXT NOT NULL,
    recipient_name TEXT NOT NULL,
    amount REAL NOT NULL
);

-- Completed company-years make genuine zero activity visible. An incomplete
-- cache is rejected before this table or any of the views are published.
CREATE TABLE coverage (
    company TEXT NOT NULL,
    grp TEXT NOT NULL,
    fy INTEGER NOT NULL,
    PRIMARY KEY (company, fy)
);

CREATE TABLE pull_metadata (
    company TEXT NOT NULL,
    fy INTEGER NOT NULL,
    pricing_code TEXT NOT NULL,
    pulled_at TEXT NOT NULL,
    PRIMARY KEY (company, fy, pricing_code)
);

CREATE TABLE recipient_exclusions (
    company TEXT NOT NULL,
    fy INTEGER NOT NULL,
    pricing_code TEXT NOT NULL,
    recipient_name TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL
);

CREATE VIEW by_company_year_group AS
SELECT company, grp, fy, pricing_group, ROUND(SUM(amount), 2) AS amount
FROM obligations
GROUP BY company, grp, fy, pricing_group;

-- Share numerator and denominator exclude negative net pricing-group amounts.
-- A company-year with no positive group total is undefined (NULL), not 0%.
CREATE VIEW share_fixed AS
WITH company_year AS (
    SELECT c.company, c.grp, c.fy,
           ROUND(COALESCE(SUM(CASE WHEN g.pricing_group = 'fixed' THEN g.amount ELSE 0 END), 0), 2) AS fixed_group_net,
           ROUND(COALESCE(SUM(g.amount), 0), 2) AS total_amount,
           ROUND(COALESCE(SUM(CASE WHEN g.amount > 0 THEN g.amount ELSE 0 END), 0), 2) AS positive_group_total
    FROM coverage c
    LEFT JOIN by_company_year_group g ON g.company = c.company AND g.fy = c.fy
    GROUP BY c.company, c.grp, c.fy
)
SELECT company, grp, fy,
       MAX(fixed_group_net, 0) AS fixed_amount,
       fixed_group_net, total_amount, positive_group_total,
       MAX(fixed_group_net, 0) / NULLIF(positive_group_total, 0) AS share_fixed
FROM company_year;
