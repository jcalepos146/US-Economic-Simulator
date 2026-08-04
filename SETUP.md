# MACROSCOPE calendar setup

1. Copy the included files and folders into the root of the GitHub Pages repository.
2. Create a free FRED API key.
3. In GitHub, open **Settings → Secrets and variables → Actions**.
4. Add a repository secret named `FRED_API_KEY`.
5. Open **Settings → Pages** and set **Source** to **GitHub Actions**.
6. Open **Actions**, select **Update economic calendar and deploy Pages**, and run it once.

The browser never receives the FRED key. GitHub Actions uses it to regenerate
`data/economic-calendar.json`, then deploys the resulting static site.

The checked-in empty JSON file is intentional: it lets the page render a clear
setup message before the first successful workflow run.
