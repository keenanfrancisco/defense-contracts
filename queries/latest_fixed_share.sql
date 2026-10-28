-- Chart 2: bind :latest_fy to the latest fully ended FY in the cached period.
-- Denominator includes every configured pricing group; zero totals stay NULL.
SELECT company, grp, fy, fixed_amount, fixed_group_net, total_amount, positive_group_total, share_fixed
FROM share_fixed
WHERE fy = :latest_fy
ORDER BY share_fixed DESC, company;
