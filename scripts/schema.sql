DROP SCHEMA IF EXISTS aleksandr_kim CASCADE;
CREATE SCHEMA aleksandr_kim;
SET search_path = aleksandr_kim;

CREATE TABLE users (
    id            serial PRIMARY KEY,
    login         text NOT NULL UNIQUE,
    name          text NOT NULL,
    role          text NOT NULL CHECK (role IN ('user', 'staff')),
    salt          text NOT NULL,
    password_hash text NOT NULL
);

CREATE TABLE categories (
    id                     serial PRIMARY KEY,
    name                   text NOT NULL UNIQUE,
    reaction_norm_minutes  integer NOT NULL
);

CREATE TABLE tickets (
    id           serial PRIMARY KEY,
    title        text NOT NULL,
    body         text NOT NULL,
    category_id  integer NOT NULL REFERENCES categories(id),
    author_id    integer NOT NULL REFERENCES users(id),
    status       text NOT NULL CHECK (status IN ('new', 'in_progress', 'closed')),
    created_at   timestamptz NOT NULL,
    closed_at    timestamptz
);

CREATE TABLE comments (
    id          serial PRIMARY KEY,
    ticket_id   integer NOT NULL REFERENCES tickets(id),
    author_id   integer NOT NULL REFERENCES users(id),
    body        text NOT NULL,
    created_at  timestamptz NOT NULL
);

-- индексы на внешние ключи (разрешены на первом этапе: Postgres сам их не создаёт)
CREATE INDEX tickets_category_id_idx ON tickets (category_id);
CREATE INDEX tickets_author_id_idx   ON tickets (author_id);
CREATE INDEX comments_ticket_id_idx  ON comments (ticket_id);
CREATE INDEX comments_author_id_idx  ON comments (author_id);
