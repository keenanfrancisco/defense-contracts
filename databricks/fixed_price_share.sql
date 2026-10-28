-- Databricks SQL: fixed-price share by company for the latest fiscal year.
-- Upload databricks/obligations.csv as a table named `obligations` first
-- (Catalog > Create table > upload file), then run this in the SQL editor.
-- Same rule as the dashboard: max(fixed, 0) / sum of positive pricing-group totals.
WITH by_group AS (
  SELECT company, grp, fy, pricing_group, SUM(amount) AS net
  FROM obligations
  GROUP BY company, grp, fy, pricing_group
),
shares AS (
  SELECT company, grp, fy,
         GREATEST(SUM(CASE WHEN pricing_group = 'fixed' THEN net ELSE 0 END), 0)
           / NULLIF(SUM(GREATEST(net, 0)), 0) AS share_fixed
  FROM by_group
  GROUP BY company, grp, fy
)
SELECT company, grp, ROUND(share_fixed * 100, 1) AS pct_fixed_price
FROM shares
WHERE fy = (SELECT MAX(fy) FROM obligations)
ORDER BY pct_fixed_price DESC;
