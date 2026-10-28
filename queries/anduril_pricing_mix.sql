-- Chart 4: keep negative modifications in their original pricing groups.
SELECT fy, pricing_group, amount
FROM by_company_year_group
WHERE company = 'Anduril'
ORDER BY fy, pricing_group;
