# Ponygram v2

Public Telegram catalog for movies, TV, songs and books. Private chat and inline queries share one search/detail/card pipeline.

- `/start`, `/help`, `/movie`, `/tv`, `/music`, `/book` are public private-chat commands.
- Inline: `@bot movie|tv|music|book keyword`. Enable inline mode in BotFather if not already enabled.
- Music and book searches always offer candidates; selecting one displays the detail card. `/movie` and `/tv` send one FilmSpot card for a single hit, and the same card after a pick when several titles match.
- TMDB supplies movies/TV (API key required). iTunes Search supplies songs (no key; configurable storefront).
- Google Books and Open Library supply books. ISBN identifies cross-source matches; edition metadata is never merged solely by title. Cards link to Goodreads search.
- Optional DeepSeek translates foreign descriptions and RSS content. `/movie` and `/tv` selling points use that same key: 1–3 short Chinese lines, or the tagline, rating, and popularity when the key is missing or the call fails. Titles and factual catalog fields stay sourced.
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

Music artwork requests 1200px CDN images. `/movie`, `/tv`, and inline `movie` / `tv` share one FilmSpot card: 片名, 原名 (only when it differs), 类型, 主创, 档期, 梗概, 卖点, and 标签. Genres and credits come from the TMDB details request, zh-CN first. Native Chinese synopses stay verbatim; foreign synopses are translated in full. A photo caption stays within Telegram's 1024 UTF-16 limit, and 卖点 / 标签 move to a follow-up message when they do not fit.
