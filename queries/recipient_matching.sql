-- Every excluded name, with the queried cohort and its net obligations.
-- Values can overlap across fuzzy searches and must not be summed as a universe.
SELECT company, recipient_name, ROUND(SUM(amount), 2) AS amount,
       MIN(fy) AS first_fy, MAX(fy) AS last_fy, COUNT(*) AS result_rows
FROM recipient_exclusions
GROUP BY company, recipient_name
ORDER BY company, ABS(SUM(amount)) DESC, recipient_name;
