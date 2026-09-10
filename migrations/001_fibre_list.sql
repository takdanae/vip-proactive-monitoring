CREATE TABLE IF NOT EXISTS fibre_list (
    fibre_id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    mesh INTEGER NOT NULL
        CHECK (typeof(mesh) = 'integer' AND mesh >= 0),
    playbox INTEGER NOT NULL
        CHECK (typeof(playbox) = 'integer' AND playbox >= 0)
);
