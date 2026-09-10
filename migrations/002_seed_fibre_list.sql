INSERT INTO fibre_list (fibre_id, name, mesh, playbox)
VALUES
    ('8804133194', 'john', 1, 1),
    ('8801478464', 'mike', 1, 1),
    ('8806756368', 'oven', 1, 1),
    ('8806916302', 'sucy', 1, 1)
ON CONFLICT(fibre_id) DO NOTHING;
