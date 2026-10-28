-- Chart 1: net transaction obligations for the configured recipient cohorts.
SELECT fy, grp, ROUND(SUM(total_amount), 2) AS amount
FROM share_fixed
GROUP BY fy, grp
ORDER BY fy, grp;
