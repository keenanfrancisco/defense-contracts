WITH company_totals AS (
    SELECT c.company, c.grp,
           ROUND(COALESCE(SUM(g.amount), 0), 2) AS total_net,
           ROUND(COALESCE(SUM(MAX(g.amount, 0)), 0), 2) AS positive_group_total,
           ROUND(COALESCE(SUM(MIN(g.amount, 0)), 0), 2) AS negative_adjustment,
           ROUND(COALESCE(SUM(CASE WHEN g.pricing_group = 'fixed' THEN g.amount ELSE 0 END), 0), 2) AS fixed_group_net
    FROM coverage c
    LEFT JOIN by_company_year_group g ON g.company = c.company AND g.fy = c.fy
    WHERE c.fy = :latest_fy
    GROUP BY c.company, c.grp
)
SELECT company, grp, total_net, positive_group_total, negative_adjustment, fixed_group_net,
       MAX(fixed_group_net, 0) AS fixed_numerator,
       MAX(fixed_group_net, 0) / NULLIF(positive_group_total, 0) AS share_fixed
FROM company_totals
ORDER BY grp, company;
