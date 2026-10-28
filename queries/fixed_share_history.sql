-- Chart 3: compare company trajectories without averaging recipient shares.
SELECT company, grp, fy, fixed_amount, fixed_group_net, total_amount, positive_group_total, share_fixed
FROM share_fixed
ORDER BY company, fy;
