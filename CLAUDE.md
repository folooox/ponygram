# Ponygram v2 development notes

See README.md for current scope. Only four public catalog categories and admin-configured RSS remain.

Business logic is in services/catalog.py. Telegram adapters are modules/catalog.py and modules/start.py. RSS delivery is modules/rss.py. Admin routes are web/app.py.

Never send RSS to personal users. Preserve source facts; AI may translate/polish descriptive content only. Keep private/inline logic shared. Test changes against an isolated database; never start another polling bot with the production token.

Run tests using the candidate image as documented in README.md. Preserve the island theme and existing production data. Backups are outside the checkout.
