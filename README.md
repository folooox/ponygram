# Ponygram v2

Public Telegram catalog for movies, TV, songs and books. Private chat and inline queries share one search/detail/card pipeline.

- `/start`, `/help`, `/movie`, `/tv`, `/music`, `/book` are public private-chat commands.
- Inline: `@bot movie|tv|music|book keyword`. Enable inline mode in BotFather if not already enabled.
- Private searches always offer candidates; selecting one displays the detail card.
- TMDB supplies movies/TV (API key required). iTunes Search supplies songs (no key; configurable storefront).
- Google Books and Open Library supply books. ISBN identifies cross-source matches; edition metadata is never merged solely by title. Cards link to Goodreads search.
- Optional DeepSeek translates/polishes descriptions and RSS content. Titles and factual catalog fields remain sourced; failure falls back to the original text.
- RSS is admin-owned and targets only channels/groups. No user private-message subscriptions. Blank destinations do not deliver. First fetch establishes a deduplication baseline; each poll sends at most five new entries, oldest first.

## Admin

`/`: overview; `/users`: searchable user list with private/inline entry records and pagination; `/queries`: category switches and shared-service preview; `/rss`: feed/destination/AI/pause/edit/delete; `/settings`: TMDB, storefront and global AI; `/ai-stats`: usage.

Configure `BOT_TOKEN`, `OWNER_ID`, `WEB_ENABLED=true`, `WEB_SECRET`, `WEB_PORT` in `.env`. Existing SQLite data and API keys are retained. RSS destination and AI flag are additive database migrations. Obsolete tables remain for recovery but have no active routes/handlers.

```sh
docker compose up -d --build
```

Validation uses the same slim runtime as production:

```sh
docker build -t ponygram-refactor-candidate .
docker run --rm ponygram-refactor-candidate python -m unittest discover -s tests -v
```

The pre-refactor source, database and image are backed up outside this checkout.

The global AI enable switch and per-feed AI flags are both required for RSS AI processing. Disabling a query category affects private, inline and admin search together. Provider requests are cached; iTunes is paced to its documented ~20 requests/minute.

Music artwork requests 1200px CDN images. Film/TV cards include source credits and preserve full synopses; native Chinese skips AI, foreign descriptions are translated without summarization.
